# SPDX-License-Identifier: AGPL-3.0-only
"""MiMo-TTS 作为**独立第 4 个配音引擎**接入 OCV（与 Qwen-TTS 并列，不覆盖）。

## 为什么需要这个模块

改动前，``patches._patch_qwen_tts()`` 直接**替换**了
``backend.app.qwen_tts.synthesize_to_file``，于是「Qwen-TTS」这个选项实际调的是
MiMo —— **两个引擎无法共存**。用户要求「同时看见两个选项」，所以必须把
「顶替」改成「并列」。

## 并列需要打中的位置（全部读源码得出，行号见 DEVLOG §2.1.3）

1. ``main.py:173`` —— ``tts_engine`` 是 Pydantic ``Literal``，新值 ``mimo`` 会被 422。
2. ``main.py:1537`` —— preflight 的 ``else`` 分支会把 ``mimo`` 当 IndexTTS-2.5，
   在无 N 卡的机器上报「本地 TTS 未就绪」。
3. ``pipeline.py:4641`` —— 派发白名单，``mimo`` 会被**强制回落** ``indextts25``。
4. ``module1_agent_director.py:78`` —— argparse ``choices=("indextts25","qwen")``
   会**直接拒绝** ``mimo``。

## 关键设计一：扩展 Literal 光靠 ``model_rebuild`` 是无效的

探针实测（FastAPI 0.116.2 / Pydantic 2.11.9，与真机同版本）：

================================  ==========================================
手段                              ``POST /api/jobs {"tts_engine":"mimo"}``
================================  ==========================================
基线                              422
改注解 + ``model_fields``          422
再 ``model_rebuild(force=True)``   **仍然 422** ← 反直觉
再重建路由 ``_type_adapter``        **200 ✅**
================================  ==========================================

**根因**：``model_rebuild`` 只刷新**类自身**的 core schema；FastAPI 在**路由注册时**
已独立捕获了一个 ``TypeAdapter``（持有旧 schema 副本）。必须
``route.dependant.body_params[]._type_adapter = pydantic.TypeAdapter(bf.type_)``
才真正生效。只做 rebuild 会表现为「补丁装上了、请求仍 422」且**无任何日志**。
登记为 DEVLOG **铁律 58**。

## 关键设计二：子进程以 ``qwen`` 身份运行，只换合成实现

``module1`` 的 ``--tts-engine`` 是 argparse ``choices``，**改不了**（OCV 源码）。
所以不传 ``mimo``，而是：

* 父进程在 ``run_pipeline`` 入口把 ``request["tts_engine"]`` 由 ``mimo`` **归一化为
  ``qwen``**（原值存进 ``_cloud_stack_tts_engine`` 留痕）；
* 子进程拿到的仍是 ``--tts-engine qwen`` ⇒ 复用**整条已验收的云端配音链路**
  （断句 ``dynamic_chunk_text``、``[QWEN_TTS_PROGRESS]`` 进度解析、逐句下载合并、
  结构化留白）—— 一分钱新代码都不用写；
* 同时给子进程打环境变量 ``CLOUD_STACK_MIMO_ACTIVE=1``，子进程内的 import 钩子据此
  把 ``synthesize_to_file`` 换成 MiMo 实现。

> **取巧但正确**：进度上报、断句、合并、字幕生成全部与「哪家云 TTS」无关，
> 换个实现即可；而 argparse 的 ``choices`` 是硬约束，绕不过去也不该绕。

## 关键设计三：按值分流，而不是替换

``module1_agent_director`` 用 ``from backend.app.qwen_tts import synthesize_to_file``
**冻结**了函数名（铁律 29），且替换是**全局**语义（必然吃掉原选项，铁律 59）。
所以只在 ``CLOUD_STACK_MIMO_ACTIVE=1`` 时才动作；选 ``qwen`` 时**一行都不碰**
``qwen_tts`` ⇒ Qwen 恢复 OCV 原生 DashScope。
"""

from __future__ import annotations

import os
import sys
from typing import Any

from . import config

ENGINE_NAME = "mimo"
ACTIVE_ENV = "CLOUD_STACK_MIMO_ACTIVE"
MARKER_KEY = "_cloud_stack_tts_engine"

_PATCHED = False
_RUNTIME_APPLIED = False


def _log(message: str) -> None:
    try:
        print(f"[cloud_free_stack] {message}", flush=True)
    except Exception:  # noqa: BLE001
        pass


def enabled() -> bool:
    """独立引擎模式开关。

    ``1``（默认）= MiMo 与 Qwen **并列**；``0`` = 退回 1.9.x 的「MiMo 顶替 Qwen」旧行为。
    """
    return config.mimo_engine_enabled()


def is_active() -> bool:
    """当前进程是否应把合成分流到 MiMo（读环境变量，子进程才能看见）。"""
    return str(os.getenv(ACTIVE_ENV) or "").strip() == "1"


# ---------------------------------------------------------------------------
# 1) 扩展 Pydantic Literal —— 必须连路由的 TypeAdapter 一起重建
# ---------------------------------------------------------------------------

def _literal_args(annotation: Any) -> tuple[str, ...]:
    import typing

    if typing.get_origin(annotation) is not typing.Literal:
        return ()
    return tuple(str(value) for value in typing.get_args(annotation) if isinstance(value, str))


def _rebuild_route_adapters(app: Any) -> list[str]:
    """重建每个路由体字段的 ``_type_adapter``（**不可省**，见模块文档）。"""
    import pydantic

    rebuilt: list[str] = []
    for route in getattr(app, "routes", []) or []:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for body_field in getattr(dependant, "body_params", []) or []:
            inner = getattr(body_field, "type_", None)
            if inner is None:
                continue
            try:
                body_field._type_adapter = pydantic.TypeAdapter(inner)
            except Exception:  # noqa: BLE001 - 单条失败不影响其它路由
                continue
            path = getattr(route, "path", "")
            if path:
                rebuilt.append(path)
    return rebuilt


def _extend_request_literal() -> list[str]:
    """把 ``mimo`` 加进 ``GenerateRequest.tts_engine`` 的允许取值（幂等）。"""
    try:
        from backend.app import main as main_module
    except Exception:  # noqa: BLE001 - 子进程没有这个模块，正常
        return []

    model = getattr(main_module, "GenerateRequest", None)
    app = getattr(main_module, "app", None)
    if model is None or app is None:
        return []

    field = getattr(model, "model_fields", {}).get("tts_engine")
    if field is None:
        return []

    if ENGINE_NAME not in _literal_args(field.annotation):
        args = _literal_args(field.annotation)
        if not args:
            _log("tts_engine 注解不是 Literal，跳过扩展（OCV 版本可能已变化）")
            return _rebuild_route_adapters(app)
        try:
            from typing import Literal

            new_annotation = Literal[tuple(args + (ENGINE_NAME,))]  # type: ignore[valid-type]
            model.__annotations__["tts_engine"] = new_annotation
            field.annotation = new_annotation
            model.model_rebuild(force=True)
            _log(f"已把 '{ENGINE_NAME}' 加入 tts_engine 允许取值")
        except Exception as exc:  # noqa: BLE001
            _log(f"扩展 tts_engine Literal 失败：{type(exc).__name__}: {exc}")
            return []

    # 无论本次是否新扩展，都要重建 adapter —— 进程重启后它们又是旧的了。
    return _rebuild_route_adapters(app)


# ---------------------------------------------------------------------------
# 2) 端点包装：门禁 + preflight
# ---------------------------------------------------------------------------

def _wrap_route(app: Any, path: str, factory: Any, *, method: str = "POST") -> bool:
    """包装某条路由的端点函数，**按 path + HTTP 方法双重匹配**。

    ⚠ 必须带方法匹配：OCV 里同一个 path 会挂多条路由 —— 例如 ``/api/jobs``
    既有 ``POST create_job``（带 ``GenerateRequest`` 体）又有 ``GET list_jobs``
    （签名完全不同）。只按 path 匹配会把 GET 那条也包上，用请求体门禁的包装层
    去套一个列表端点（自检实测抓出来的，见 DEVLOG 1.10.0 验证记录）。
    """
    for route in getattr(app, "routes", []) or []:
        if getattr(route, "path", "") != path:
            continue
        methods = {str(item).upper() for item in (getattr(route, "methods", None) or set())}
        if method.upper() not in methods:
            continue
        dependant = getattr(route, "dependant", None)
        original = getattr(dependant, "call", None) if dependant is not None else None
        if original is None or getattr(original, "_cloud_stack_mimo_wrapped", False):
            continue
        wrapped = factory(original)
        wrapped._cloud_stack_mimo_wrapped = True  # type: ignore[attr-defined]
        dependant.call = wrapped
        return True
    return False


def _guard_create_job(original: Any) -> Any:
    """提交门禁：``mimo`` 需要 MiMo Key（而不是 DashScope 的存在性）。"""

    def wrapper(payload: Any, *args: Any, **kwargs: Any) -> Any:
        from fastapi import HTTPException

        engine = str(getattr(payload, "tts_engine", "") or "").strip().lower()
        if engine == ENGINE_NAME and not config.mimo_api_key():
            raise HTTPException(
                status_code=400,
                detail=(
                    "MiMo-TTS 尚未配置 API Key，请先在界面右下角「云端免费栈」"
                    "面板中填写 MiMo API Key"
                ),
            )
        return original(payload, *args, **kwargs)

    return wrapper


def _guard_preflight(original: Any) -> Any:
    """preflight：给 ``mimo`` 一条自己的就绪项（否则会落进 IndexTTS 的「未就绪」）。"""

    def wrapper(payload: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(payload, *args, **kwargs)
        if str(getattr(payload, "tts_engine", "") or "").strip().lower() != ENGINE_NAME:
            return result
        if not isinstance(result, dict):
            return result
        items = result.get("items")
        if not isinstance(items, list):
            return result
        # 摘掉与 mimo 无关的 IndexTTS/GPU 项
        items[:] = [
            item for item in items
            if not (isinstance(item, dict) and item.get("key") in {"tts", "gpu"})
        ]
        has_key = bool(config.mimo_api_key())
        items.append({
            "key": "tts",
            "label": "MiMo TTS",
            "status": "passed" if has_key else "error",
            "message": (
                f"MiMo API Key 已配置；模型 {config.mimo_model()}，"
                f"默认音色 {config.mimo_default_voice()}"
                if has_key
                else "尚未配置 MiMo API Key（界面右下角「云端免费栈」面板）"
            ),
        })
        return result

    return wrapper


# ---------------------------------------------------------------------------
# 3) 归一化 + 子进程环境注入
# ---------------------------------------------------------------------------

def normalize_request(request: dict[str, Any]) -> None:
    """把 ``tts_engine=mimo`` 归一化成 ``qwen``（原值留痕）。

    这样整条云端配音链路（白名单、argv、进度正则、归档）都能原样复用，
    而 argparse 的 ``choices`` 也不会拒绝。
    """
    if str(request.get("tts_engine") or "").strip().lower() != ENGINE_NAME:
        return
    request[MARKER_KEY] = ENGINE_NAME
    request["tts_engine"] = "qwen"


def wants_mimo(job: Any) -> bool:
    """该任务是否走 MiMo（看留痕，或还没归一化时的原值）。"""
    request = getattr(job, "request", None)
    if not isinstance(request, dict):
        return False
    if str(request.get(MARKER_KEY) or "").strip().lower() == ENGINE_NAME:
        return True
    return str(request.get("tts_engine") or "").strip().lower() == ENGINE_NAME


def _patch_run_pipeline() -> bool:
    """在 ``run_pipeline`` 入口归一化引擎值（模块全局名可包装，已实证）。

    原函数**惰性取自** ``wrapper._cloud_stack_original``，而不是闭包捕获：
    这样自检可以只替换那一格来观测包装层行为，不需要真跑流水线。
    """
    try:
        from backend.app import pipeline as pipeline_module
    except Exception:  # noqa: BLE001
        return False
    original = getattr(pipeline_module, "run_pipeline", None)
    if not callable(original):
        return False
    if getattr(original, "_cloud_stack_mimo_wrapped", False):
        return True

    def wrapper(job: Any, store: Any, **kwargs: Any) -> Any:
        request = getattr(job, "request", None)
        if isinstance(request, dict):
            normalize_request(request)
        return wrapper._cloud_stack_original(job, store, **kwargs)  # type: ignore[attr-defined]

    wrapper._cloud_stack_mimo_wrapped = True  # type: ignore[attr-defined]
    wrapper._cloud_stack_original = original  # type: ignore[attr-defined]
    pipeline_module.run_pipeline = wrapper  # type: ignore[assignment]
    return True


def _is_tts_command(command: Any) -> bool:
    """这条命令是不是「模块 1 配音」？

    只给配音子进程打水印，而不是该任务的**所有**子进程 —— 模块 2/4/5 与 TTS
    无关，没必要让它们的进程里也挂上会替换合成实现的开关（少一个变量少一份意外）。
    """
    try:
        parts = [str(item) for item in command]
    except TypeError:
        return False
    return any(part.endswith("module1_agent_director.py") for part in parts)


def _patch_run_command() -> bool:
    """在 ``run_command`` 给 MiMo 的**配音**子进程打 ``CLOUD_STACK_MIMO_ACTIVE``。

    与 ``_patch_run_pipeline`` 同样惰性取原函数，便于自检替换观测。
    """
    try:
        from backend.app import pipeline as pipeline_module
    except Exception:  # noqa: BLE001
        return False
    original = getattr(pipeline_module, "run_command", None)
    if not callable(original):
        return False
    if getattr(original, "_cloud_stack_mimo_wrapped", False):
        return True

    def wrapper(job: Any, store: Any, command: Any, label: Any, **kwargs: Any) -> Any:
        if wants_mimo(job) and _is_tts_command(command):
            extra_env = dict(kwargs.get("extra_env") or {})
            extra_env[ACTIVE_ENV] = "1"
            kwargs["extra_env"] = extra_env
        return wrapper._cloud_stack_original(job, store, command, label, **kwargs)  # type: ignore[attr-defined]

    wrapper._cloud_stack_mimo_wrapped = True  # type: ignore[attr-defined]
    wrapper._cloud_stack_original = original  # type: ignore[attr-defined]
    pipeline_module.run_command = wrapper  # type: ignore[assignment]
    return True


def _patch_tts_editor() -> bool:
    """逐句精配 / 重配走的是 ``tts_editor`` 自己的 subprocess，同样要打水印。

    这两个方法都是**同步阻塞**的，所以用 ``try/finally`` 包住环境变量是安全的
    （子进程在方法内部启动并 wait 完成）。
    """
    try:
        from backend.app import tts_editor as editor_module
    except Exception:  # noqa: BLE001
        return False
    editor_cls = getattr(editor_module, "TtsEditor", None)
    if editor_cls is None:
        return False

    def _wrap_method(name: str) -> bool:
        original = getattr(editor_cls, name, None)
        if not callable(original) or getattr(original, "_cloud_stack_mimo_wrapped", False):
            return callable(original)

        def wrapper(self: Any, job: Any, *args: Any, **kwargs: Any) -> Any:
            if not wants_mimo(job):
                return original(self, job, *args, **kwargs)
            previous = os.environ.get(ACTIVE_ENV)
            os.environ[ACTIVE_ENV] = "1"
            try:
                return original(self, job, *args, **kwargs)
            finally:
                if previous is None:
                    os.environ.pop(ACTIVE_ENV, None)
                else:
                    os.environ[ACTIVE_ENV] = previous

        wrapper._cloud_stack_mimo_wrapped = True  # type: ignore[attr-defined]
        wrapper._cloud_stack_original = original  # type: ignore[attr-defined]
        setattr(editor_cls, name, wrapper)
        return True

    ok_a = _wrap_method("_synthesize_parts")
    ok_b = _wrap_method("_regenerate_sync")
    return ok_a and ok_b


# 注：**不需要**给 `_api_key_status()` 的 `qwen_tts` 格打补丁。
# 面板里的 MiMo Key 已由 `bootstrap.apply_env_aliases()` 顶进
# `DASHSCOPE_API_KEY`，所以那一格天然就是「MiMo Key 是否已配置」，语义自洽；
# 再叠一层只会让两个来源打架。


# ---------------------------------------------------------------------------
# 4) 子进程分流：把合成实现换成 MiMo（仅当本进程被标记为 active）
# ---------------------------------------------------------------------------

def apply_child_runtime() -> bool:
    """在**子进程**内把 ``synthesize_to_file`` 分流到 MiMo。

    由 ``patches._patch_qwen_tts`` 在 ``backend.app.qwen_tts`` 加载完成后调用。
    只有 ``CLOUD_STACK_MIMO_ACTIVE=1`` 时才动作 —— 这是「并列而非覆盖」的关键：
    选 qwen 时这里一行都不做。
    """
    global _RUNTIME_APPLIED
    if _RUNTIME_APPLIED:
        return True
    if not is_active():
        return False
    try:
        from backend.app import qwen_tts as qwen_module

        from . import mimo_tts
    except Exception as exc:  # noqa: BLE001
        _log(f"MiMo 分流失败（模块不可用）：{type(exc).__name__}: {exc}")
        return False

    qwen_module.synthesize_to_file = mimo_tts.synthesize_to_file  # type: ignore[assignment]
    qwen_module.voice_supports_instructions = _mimo_supports_instructions  # type: ignore[assignment]

    # module1 用 from-import 冻结了这两个名字，必须连它的命名空间一起换（铁律 29）
    module1 = sys.modules.get("module1_agent_director")
    if module1 is not None:
        module1.synthesize_to_file = mimo_tts.synthesize_to_file  # type: ignore[attr-defined]
        module1.voice_supports_instructions = _mimo_supports_instructions  # type: ignore[attr-defined]

    _RUNTIME_APPLIED = True
    return True


def _mimo_supports_instructions(voice: str) -> bool:  # noqa: ARG001
    """MiMo 的预置音色始终可以与风格指令一起使用。"""
    return True


# ---------------------------------------------------------------------------
# 5) 总安装
# ---------------------------------------------------------------------------

def install() -> bool:
    """在后端进程里安装「独立引擎」所需的全部补丁（幂等）。"""
    global _PATCHED
    if _PATCHED:
        return True
    if not config.enabled() or not enabled():
        return False
    try:
        rebuilt = _extend_request_literal()
        _patch_endpoint_calls()
        _patch_run_pipeline()
        _patch_run_command()
        _patch_tts_editor()
        if rebuilt:
            _log(f"已重建 {len(rebuilt)} 条路由的请求校验器（含 tts_engine 扩展）")
    except Exception as exc:  # noqa: BLE001 - 插件绝不能拖垮 OCV
        _log(f"独立引擎安装失败，将保持 OCV 原生行为：{type(exc).__name__}: {exc}")
        return False
    _PATCHED = True
    return True


def _patch_endpoint_calls() -> None:
    try:
        from backend.app import main as main_module
    except Exception:  # noqa: BLE001
        return
    app = getattr(main_module, "app", None)
    if app is None:
        return
    _wrap_route(app, "/api/jobs", _guard_create_job)
    _wrap_route(app, "/api/jobs/preflight", _guard_preflight)


def status() -> dict[str, Any]:
    """给自检脚本用的运行时快照。"""
    return {
        "enabled": enabled(),
        "active_in_this_process": is_active(),
        "installed": _PATCHED,
        "runtime_applied": _RUNTIME_APPLIED,
    }
