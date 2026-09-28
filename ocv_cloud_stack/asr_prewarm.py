"""ASR 运行时的预热与诊断。

模块 2 的第一件事是自检 —— ``backend/app/pipeline.py::_asr_runtime_available``
跑 ``python -c "import ctranslate2, faster_whisper"``，**超时只有 90 秒**。
这台机器的整合包在这条路径上有个和插件无关、但会直接打死任务的结构性成本：

* ``ctranslate2/__init__.py`` 的最后一行是
  ``from ctranslate2 import converters, models, specs``；
* ``converters`` 会连带拉起 ``torch`` 与 ``transformers``；
* 本整合包里的 torch 是 ``2.8.0+cu128``，``torch/lib`` 有 **6.9 GB / 37 个 DLL**；
* 第一次加载这批 CUDA DLL（OS 文件缓存冷 + Defender 实时扫描 7 GB）可以轻松
  超过 90 秒；被扫过之后，同一个导入只要 6 秒级。

实测（同一台机器）：

======================  ==========
条件                    导入耗时
======================  ==========
缓存已热                5.7 s
字节码缓存全冷          28.1 s
======================  ==========

所以本模块做的是**把这笔钱提前付掉**：在 OCV 后端启动时，用一个一次性子进程
把同样的导入跑一遍。这样做有两个刻意的取舍：

1. 用**子进程**而不是后端进程内导入 —— 7 GB DLL 不该长驻在负责界面的后端里；
2. 结果只记日志，**绝不阻塞、绝不抛出** —— 预热失败时模块 2 仍按原样自检。

另配 ``cloud_stack_ctl.py prewarm`` 供手工触发（例如刚重启完机器，
想先把冷启动成本付掉再跑任务）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config

# 与 pipeline.py::_asr_runtime_available 保持**逐字一致**，否则预热的东西
# 和自检实际要导入的东西不是一份，白付成本。
_IMPORT_SNIPPET = "import ctranslate2, faster_whisper"

_LOCK = threading.Lock()
_STARTED = False


def _plugin_log(message: str) -> None:
    from . import patches

    patches.log(message)


def _plugin_debug(message: str) -> None:
    from . import patches

    patches.debug(message)


def enabled() -> bool:
    raw = config.get("CLOUD_STACK_ASR_PREWARM", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def timeout_seconds() -> float:
    raw = config.get("CLOUD_STACK_ASR_PREWARM_TIMEOUT", "900").strip()
    try:
        value = float(raw)
    except ValueError:
        return 900.0
    return max(60.0, value)


def asr_python() -> str:
    """模块 2 实际会用的解释器。

    与 ``pipeline.resolve_asr_python()`` 同源：优先 ``ASR_PYTHON``
    （``start_backend_dev.bat`` 会把它设成便携解释器），否则当前解释器。
    """
    configured = os.getenv("ASR_PYTHON", "").strip()
    if configured and Path(configured).is_file():
        return configured
    return sys.executable


def probe(timeout: float | None = None) -> tuple[bool, float, str]:
    """跑一次与模块 2 完全相同的导入。返回 ``(是否成功, 耗时秒, 错误摘要)``。"""
    limit = timeout if timeout is not None else timeout_seconds()
    started = time.perf_counter()
    try:
        result = subprocess.run(
            [asr_python(), "-c", _IMPORT_SNIPPET],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=limit,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, time.perf_counter() - started, f"超过 {int(limit)} 秒仍未完成"
    except OSError as exc:
        return False, time.perf_counter() - started, str(exc)

    elapsed = time.perf_counter() - started
    if result.returncode == 0:
        return True, elapsed, ""
    return False, elapsed, (result.stderr or "").strip()[:400] or f"退出码 {result.returncode}"


def describe(elapsed: float) -> str:
    """把耗时翻译成人话，方便判断缓存到底热没热。"""
    if elapsed < 20:
        return "已是热缓存"
    if elapsed < 60:
        return "偏慢，可能刚被系统清理过"
    return "冷启动（首次加载 CUDA DLL 的正常代价）"


def prewarm_async() -> None:
    """后台线程预热，立即返回。同一进程内只启动一次。"""
    global _STARTED
    if not enabled():
        _plugin_debug("ASR 预热已关闭（CLOUD_STACK_ASR_PREWARM=0）")
        return
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=_run_prewarm, name="ocv-cloud-asr-prewarm", daemon=True).start()


def _run_prewarm() -> None:
    ok, elapsed, error = probe()
    if ok:
        _plugin_log(
            f"ASR 运行时已预热：导入 ctranslate2/faster_whisper 用时 {elapsed:.1f} 秒"
            f"（{describe(elapsed)}，模块 2 自检将命中它）"
        )
    else:
        _plugin_log(f"ASR 预热未成功（不影响运行）：{elapsed:.1f} 秒 {error}")


def report() -> int:
    """给 CLI 用的同步版本：跑一次并把结论打出来。"""
    interpreter = asr_python()
    limit = timeout_seconds()
    print(f"· 解释器: {interpreter}")
    print(f"· 预热上限: {int(limit)} 秒（CLOUD_STACK_ASR_PREWARM_TIMEOUT）")
    print(f"· 导入语句: {_IMPORT_SNIPPET}")
    print("· 正在预热，冷启动可能要几分钟，请勿中断……", flush=True)
    ok, elapsed, error = probe()
    print()
    if ok:
        print(f"✓ 预热完成：用时 {elapsed:.1f} 秒（{describe(elapsed)}）")
        print("  模块 2 的 90 秒自检从此走热缓存；重启机器后建议再跑一次。")
        return 0
    print(f"✗ 预热未成功：用时 {elapsed:.1f} 秒")
    print(f"  {error}")
    print("  这通常意味着 faster-whisper / ctranslate2 没装好，")
    print("  请检查 ASR_PYTHON 指向的环境，或重新安装 requirements.txt。")
    return 1
