"""插件注入总入口。

``sitecustomize.py`` 只负责调用这里的 :func:`install`。真正的注入分四步：

1. 把面板覆盖层（``var/config.json``）物化进 ``os.environ`` —— 面板是最高
   优先级配置源，而 OCV 的原生校验与所有子进程都只看环境变量，不这么做
   面板里填的 Key 就等于没填。
2. 装一层 ``builtins.__import__`` 包装 —— 这是唯一能在"OCV 的每个子进程
   都会执行、且不需要改 OCV 任何文件"的前提下挂钩到目标模块的办法。
   CPython 编译 ``from x import y`` 时会先执行 ``IMPORT_NAME x`` 再执行
   ``IMPORT_FROM y``（即 ``getattr(module, "y")``），所以只要在
   ``__import__`` 返回之前完成替换，调用方拿到的就是补丁后的对象。
3. 应用环境变量别名（MiMo Key 顶上 DashScope 的存在性校验）。
4. 检查本地图片 shim 的地址是否与 ``IMAGE_API_BASE_URL`` 一致（只查不拉起）。

shim 的实际托管交给**长驻进程**：OCV 后端（见 :func:`patches._patch_main`）
或 CLI 命令自己。注入发生在每个进程启动时，其中很多是秒级退出的短命进程，
由它们去拉分离进程只会产生一批随父进程一起消失的僵尸 —— 在受限环境里
``CREATE_BREAKAWAY_FROM_JOB`` 还会被拒绝。所以这里刻意不做拉起动作。

任何一步失败都只记日志，绝不抛出 —— 插件坏掉不能让整个 OCV 起不来。
"""

from __future__ import annotations

import builtins
import importlib.util
import os
import sys
import threading
from types import ModuleType
from typing import Any

from . import config, console, patches, shim_launcher

_LOCK = threading.Lock()
_INSTALLED = False
_IMPORT_COUNT = 0


def _resolve_name(name: str, level: int, globals_: dict[str, Any] | None) -> str:
    """把相对导入的模块名还原成绝对名。"""
    if not level or not globals_:
        return name
    package = str(globals_.get("__package__") or "").strip()
    if not package:
        module_name = str(globals_.get("__name__") or "")
        package = module_name.rpartition(".")[0]
    if not package:
        return name
    try:
        return importlib.util.resolve_name("." * level + name, package)
    except (ImportError, ValueError):
        return name


def _install_import_hook() -> None:
    if getattr(builtins, "_ocv_cloud_stack_hooked", False):
        return
    original = builtins.__import__

    def _hooked_import(  # type: ignore[no-untyped-def]
        name, globals=None, locals=None, fromlist=(), level=0
    ):
        module = original(name, globals, locals, fromlist, level)
        global _IMPORT_COUNT
        _IMPORT_COUNT += 1
        try:
            if patches.has_pending():
                patches.on_import(_resolve_name(str(name), int(level or 0), globals))
        except Exception:
            pass
        return module

    builtins.__import__ = _hooked_import
    builtins._ocv_cloud_stack_hooked = True  # type: ignore[attr-defined]
    patches.debug("导入钩子已安装")


def export_store_to_environ() -> list[str]:
    """把面板层非空值写进 ``os.environ``，返回本次实际改写的键。

    面板保存（``panel.apply``）与进程启动（sitecustomize）都会走这里：
    前者让「运行中改配置」立即对**当前进程**生效 —— Agent 1 的语言模型
    调用发生在后端进程内（``pipeline`` 直接 ``import story_agents``），
    ``gemini_client.language_model()`` 调用时读的是 ``os.environ``，
    不同步环境变量的话，面板换模型永远追不上正在跑的后端。
    """
    applied: list[str] = []
    for key, cached in config.store_values().items():
        text = str(cached or "").strip()
        if not text:
            # 空值绝不写入：那会把「未配置」变成「配置成空字符串」。
            # os.getenv(key, default) 只在键**不存在**时用默认值，
            # 键存在而值为空会让下游拿到 ""，例如 module2 的
            # os.getenv("ASR_DEVICE", "auto") 会直接抛 ValueError。
            continue
        if os.environ.get(key) == text:
            continue
        os.environ[key] = text
        applied.append(key)
    return applied


def remove_store_keys_from_environ(previous: dict[str, str] | None = None) -> list[str]:
    """把一组键从 ``os.environ`` 里摘掉（面板层删除该项时用）。

    ``previous`` 是删除前的面板层键值快照。只在「面板层此前提供过非空值、
    且当前环境值仍等于该值」时才摘（避免误删用户在别处手工 set 的环境）。
    摘除后下游 ``os.getenv`` 回落到内置默认值 —— ``.env`` 的值在进程启动
    时就已定快照，运行中无法再兜底，这是已知边界。
    """
    removed: list[str] = []
    for name, old in (previous or {}).items():
        key = str(name or "").strip()
        old_text = str(old or "").strip()
        if not key or not old_text:
            continue
        if os.environ.get(key) != old_text:
            continue
        if os.environ.pop(key, None) is not None:
            removed.append(key)
    return removed


def _export_store_to_environ() -> None:
    """启动阶段的物化入口：写环境变量并打日志（见 ``export_store_to_environ``）。"""
    applied = export_store_to_environ()
    if applied:
        patches.log(f"面板覆盖层已物化到环境变量：{', '.join(applied)}")


def _ensure_shim() -> None:
    """只做地址一致性检查，不主动拉起 shim。

    拉起动作由长驻进程负责：

    * OCV 后端 —— :func:`patches._patch_main` 里用后台线程托管
      （与界面同生命周期，且不受 job object 影响）；
    * CLI 自检 / 面板 —— ``cloud_stack_ctl.py`` 在自己进程里托管。

    这样做的原因见模块文档：让秒级退出的进程去拉分离进程，产物是一堆
    随父进程消失的僵尸，反而掩盖了真实状态。
    """
    if not config.shim_autostart():
        return
    if shim_launcher.is_listening():
        _warn_if_base_mismatch()
        return
    patches.debug("shim 尚未就绪，将由 OCV 后端进程或 cloud_stack_ctl 托管")


_warned_mismatch = False
_MISMATCH_LOCK = threading.Lock()


def _warn_if_base_mismatch() -> None:
    global _warned_mismatch
    with _MISMATCH_LOCK:
        if _warned_mismatch:
            return
        _warned_mismatch = True
    expected = config.shim_base_url()
    actual = config.get("IMAGE_API_BASE_URL", "").strip().rstrip("/")
    if actual == expected:
        return
    if not actual:
        patches.log(
            f"⚠ IMAGE_API_BASE_URL 未设置，图片请求仍会打到第三方接口。"
            f"请运行 cloud_stack_ctl.py install 写入 {expected}"
        )
    elif "127.0.0.1" not in actual and "localhost" not in actual:
        patches.log(
            f"⚠ IMAGE_API_BASE_URL 指向 {actual}，不是本机 shim（{expected}）；"
            f"商汤图片链路不会生效。"
        )


def install() -> None:
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        _INSTALLED = True

    # 控制台编码加固：中文 Windows 的 stdout 是 GBK，打印 ✓ / ✗ / ⚠ 会抛
    # UnicodeEncodeError 并**终止进程**。放在所有开关判断之前 —— 哪怕插件随后
    # 被禁用或跳过，也不该让哪个进程因为一个符号崩掉。
    try:
        console.harden()
    except Exception:
        pass

    # shim 自己也是用同一个解释器拉起来的，必须跳过注入，避免递归
    if os.getenv("OCV_CLOUD_STACK_SKIP", "").strip() == "1":
        return

    try:
        if not config.enabled():
            return
    except Exception:
        return

    try:
        # 面板层必须**最先**落进 os.environ：后面的环境别名（apply_env_aliases）
        # 与所有 OCV 原生校验都读 os.environ，子进程更是只继承环境变量。
        _export_store_to_environ()
        _install_import_hook()
        patches.install_meta_hook()
        patches.apply_env_aliases()
        patches.patch_everything_ready()
    except Exception as exc:  # noqa: BLE001
        patches.log(f"注入失败，OCV 将以原生模式继续运行：{type(exc).__name__}: {exc}")
        return

    # 探活是本地 connect，失败即刻返回，不阻塞启动
    try:
        _ensure_shim()
    except Exception:
        pass


def status() -> dict[str, Any]:
    """给自检脚本用的运行时快照。"""
    return {
        "enabled": config.enabled(),
        "installed": _INSTALLED,
        "imports_seen": _IMPORT_COUNT,
        "patched_modules": sorted(patches.patched_modules()),
        "shim_listening": shim_launcher.is_listening(),
        "shim_base_url": config.shim_base_url(),
        "image_api_base_url": config.get("IMAGE_API_BASE_URL", ""),
        "mimo_model": config.mimo_model(),
        "sensenova_model": config.sensenova_model(),
        "sensenova_image_model": config.sensenova_image_model(),
    }


def current_module() -> ModuleType:
    return sys.modules[__name__]
