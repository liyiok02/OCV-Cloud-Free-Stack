"""把「插件面板里配好的 Agnes」桥给 OCV 的**原生视频门禁**（F-011）。

## 用户症状

    一键制作已暂停：请先配置视频 API。

但用户**已经在云端免费栈面板填好了 Agnes API Key**。

## 根因：两套配置之间没有桥

OCV 前端放不放行，看的是它自己的 ``GET /api/video-model``
（``frontend/src/components/VideoStudio.vue``）：

    videoConfigured = videoModel.source === 'dedicated' && videoModel.has_api_key

而 ``video_model_config.load_config()`` 只读 **``VIDEO_API_*``**（``.env`` /
``os.environ``）。插件配的是 **``AGNES_API_KEY``** —— OCV 完全不认识它。

于是：

| 视角 | 实际值 |
|---|---|
| OCV 原生门禁 | ``source='missing'``、``has_api_key=false`` ⇒ 判定**未配置** |
| 插件面板层 | ``AGNES_API_KEY`` 有值、``CLOUD_STACK_VIDEO_ENABLED='1'`` |
| 两者的桥 ``VIDEO_API_*`` | ``.env`` 里**一个都没有** |

原本只有 ``cloud_stack_ctl.py video-install`` 能架这座桥，但它**必须手工执行**
且改完要**重启** OCV。用户合理地以为"在面板里配了就该能用"，于是卡住。

## 修法：运行时桥接（免重启）

``load_config()`` 读取时 ``os.environ`` **覆盖** ``.env``。所以只要在
**调用时刻**把指向本地 shim 的 ``VIDEO_API_*`` 视图交给 OCV 即可 ——

* 不改 ``.env``（不留持久痕迹）
* 不改 OCV 源码（只包装 ``load_config``）
* **免重启**：wrapper 每次调用都实时读面板层，用户在面板一填就生效

## 两个开关都要满足（**绝不在用户没同意时改他的视频通道**）

1. ``CLOUD_STACK_VIDEO_ENABLED=1`` —— 这就是插件的"显式接管"开关，
   与 ``video-install`` 的语义完全一致（视频按秒计费，必须 opt-in）；
2. ``AGNES_API_KEY`` 非空 —— 上游凭据存在，接管才有意义。

任一不满足 ⇒ **原样返回 OCV 原生配置**，插件一行都不插手。

## 为什么顺带解决"接管后仍要重启"

包装 ``load_config`` 之后，``video_generation.py`` 拿到的也是桥接后的配置
（它 ``from .video_model_config import load_config``，见铁律 29 的绑定陷阱，
所以那个模块也要一起换），于是门禁与真实提交路径**用的是同一套值**，
不会出现"门禁说已配置、提交却打到别处"的错配。
"""

from __future__ import annotations

import os
import sys
import threading as _threading
from typing import Any

from . import config

_LOG_PREFIX = "[cloud_free_stack]"

_INSTALLED = False
_ORIGINAL: Any = None
_WRAPPER: Any = None
_BRIDGED_CALLS = 0
_WARNED = False
_LOCK = _threading.Lock()

# 交给 OCV 的 shim 路由（与 cloud_stack_ctl.py 的取值必须一致，
# 自检会用正则从那份源码里读出来逐一断言 —— 引用而非各写一份）。
SHIM_SUBMIT_PATH = "/openapi/v2/video/agnes/multimodal-video"
SHIM_QUERY_PATH = "/api/video/query"
SHIM_UPLOAD_PATH = "/openapi/v2/media/upload/binary"

# 交给 OCV 的"已配置"占位 Key。
#
# OCV 会拿它发 ``Authorization: Bearer <KEY>`` 到 **shim**，
# shim 再用面板里的真 Key 调 Agnes（与图片层把 Key 放面板同一套）。
# 真 Key 绝不进 ``.env``、也绝不进这个返回值以外的地方。
PLACEHOLDER_KEY = "managed-by-cloud-free-stack"

# 凡是 ``from .video_model_config import load_config`` 的调用方都要换（铁律 29）。
_TARGET_MODULES: tuple[str, ...] = (
    "backend.app.video_model_config",
    "backend.app.video_generation",
)


def enabled() -> bool:
    """是否该桥接：接管开关 + 上游凭据**同时**具备。"""
    try:
        if not config.video_enabled():
            return False
        return bool(str(config.video_api_key() or "").strip())
    except Exception:  # noqa: BLE001 - 配置层异常绝不能拖垮 OCV
        return False


def _log(message: str) -> None:
    try:
        print(f"{_LOG_PREFIX} {message}", flush=True)
    except Exception:  # noqa: BLE001
        pass


def bridge_config(native: dict[str, Any] | None) -> dict[str, Any]:
    """把原生配置换成"指向本地 shim"的等价视图。

    ``native`` 可以是 ``None``（原生配置缺失或抛错）—— 那种情况下本函数
    产出一份**完整可用**的配置，字段与 ``load_config()`` 的返回同构
    （``video_generation`` 会直接消费这些键，缺一个都会炸）。
    """
    base = dict(native or {})
    # 分辨率必须落在 OCV 允许的集合里，否则它自己的校验会拒。
    resolution = str(base.get("resolution") or "720p").strip().lower()
    if resolution not in {"480p", "720p"}:
        resolution = "720p"

    bridged = {
        **base,
        "base_url": config.shim_base_url(),
        "submit_path": SHIM_SUBMIT_PATH,
        "query_path": SHIM_QUERY_PATH,
        "upload_path": SHIM_UPLOAD_PATH,
        "resolution": resolution,
        "api_key": PLACEHOLDER_KEY,
        "api_keys": [PLACEHOLDER_KEY],
        "key_count": 1,
        # 不泄露任何真实凭据片段（连后四位都不给）。
        "key_hints": [],
        "concurrency_mode": "auto",
        "per_key_concurrency": 1,
        "total_concurrency": 1,
        "effective_concurrency": 1,
        "has_api_key": True,
        "source": "dedicated",
        "model_label": "Agnes 视频（cloud_free_stack 接管）",
    }
    return bridged


def _make_wrapper() -> Any:
    original = _ORIGINAL

    def _bridged_load_config(*args: Any, **kwargs: Any) -> Any:
        global _BRIDGED_CALLS, _WARNED
        native = None
        native_error: Exception | None = None
        try:
            native = original(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 原生报错时仍可能可以桥接
            native_error = exc

        if not enabled():
            if native_error is not None:
                raise native_error
            return native

        bridged = bridge_config(native if isinstance(native, dict) else None)
        with _LOCK:
            _BRIDGED_CALLS += 1
            first = not _WARNED
            _WARNED = True
        if first:
            detail = ""
            if isinstance(native, dict) and native.get("source") == "dedicated" \
                    and str(native.get("base_url") or "").rstrip("/") != config.shim_base_url():
                # 用户另外还配了真实的视频服务商。接管开关是他自己打开的，
                # 所以按接管处理，但**必须**让他看得见这件事，不能静默。
                detail = f"（已覆盖 OCV 原视频地址 {native.get('base_url')}）"
            _log(
                f"视频门禁已桥接：OCV 原生配置缺失/未生效，"
                f"已按面板里的 Agnes 配置指向本地 shim {config.shim_base_url()}，"
                f"模型 {config.video_model()}{detail}"
            )
        if native_error is not None and not isinstance(native, dict):
            # 原生抛错但我们能桥接 ⇒ 以桥接结果为准（用户的配置是有效的）。
            return bridged
        return bridged

    _bridged_load_config._cloud_stack_wrapped = True  # type: ignore[attr-defined]
    return _bridged_load_config


def install_on(module: Any) -> bool:
    """把 ``module.load_config`` 换成桥接版（幂等）。"""
    global _INSTALLED, _ORIGINAL, _WRAPPER
    current = getattr(module, "load_config", None)
    if not callable(current) or getattr(current, "_cloud_stack_wrapped", False):
        return False
    if _ORIGINAL is None:
        _ORIGINAL = current
    if _WRAPPER is None:
        _WRAPPER = _make_wrapper()
    module.load_config = _WRAPPER  # type: ignore[attr-defined]
    _INSTALLED = True
    return True


def install(*modules: Any) -> bool:
    """装到指定模块；不传则装到所有已导入的目标模块。"""
    changed = False
    if modules:
        for module in modules:
            changed = install_on(module) or changed
    else:
        for name in _TARGET_MODULES:
            module = sys.modules.get(name)
            if module is not None:
                changed = install_on(module) or changed
    if changed:
        _log(
            "已安装视频门禁桥接：面板里配好 Agnes 并打开「启用云端视频接管」后，"
            "OCV 的原生视频门禁即刻认为已配置（免重启、不改 .env）——"
            "不再出现「请先配置视频 API」"
        )
    return changed


def installed() -> bool:
    return _INSTALLED


def diagnostics() -> dict[str, Any]:
    def wrapped(name: str) -> bool:
        if _WRAPPER is None:
            return False
        return getattr(sys.modules.get(name), "load_config", None) is _WRAPPER

    return {
        "installed": _INSTALLED,
        "bridging": enabled(),
        "video_enabled": config.video_enabled(),
        "has_agnes_key": bool(str(config.video_api_key() or "").strip()),
        "bridged_calls": _BRIDGED_CALLS,
        "wrapped_modules": [name for name in _TARGET_MODULES if wrapped(name)],
        "shim_base_url": config.shim_base_url(),
        "model": config.video_model(),
    }


def uninstall(*modules: Any) -> bool:
    """撤掉桥接，恢复原生行为（自检的对照组用）。"""
    global _INSTALLED
    if _ORIGINAL is None:
        return False
    targets = modules or tuple(
        module for module in (sys.modules.get(name) for name in _TARGET_MODULES)
        if module is not None
    )
    restored = False
    for module in targets:
        if _WRAPPER is not None and getattr(module, "load_config", None) is _WRAPPER:
            module.load_config = _ORIGINAL  # type: ignore[attr-defined]
            restored = True
    _INSTALLED = False
    return restored
