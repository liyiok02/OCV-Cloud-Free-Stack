# SPDX-License-Identifier: AGPL-3.0-only
"""端到端验证：真实 FastAPI app 上，mimo 与 qwen 两条链路**确实分流**。

与 ``verify_mimo_engine.py`` 的分工：
  * 那个脚本验**单元不变量**（Literal 扩展手法、归一化、幂等…）；
  * 本脚本发**真实 HTTP 请求**走完「校验 → 门禁 → 建任务 → run_pipeline 入口」，
    证明 mimo 被接受且真的会走到 MiMo 合成，而 qwen 走原生。

**安全性**（铁律 56/57）：全程**不真跑流水线**。做法是包装 ``JobStore.run_async``
把它变成空操作（只记录被排队的请求），这样：
  * 不写用户配置；
  * 不起任何子进程（不会消耗任何 API 额度）；
  * 不产出任何用户产物。

运行：

    runtime\\python\\python.exe plugins\\cloud_free_stack\\verify_mimo_e2e.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
OCV_ROOT = PLUGIN_ROOT.parents[1]
if str(OCV_ROOT) not in sys.path:
    sys.path.insert(0, str(OCV_ROOT))

try:
    from plugins.cloud_free_stack.ocv_cloud_stack import console

    console.harden()
except Exception:  # noqa: BLE001
    pass

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    else:
        FAIL += 1
        print(f"  [!!] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return condition


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


print("启动 OCV 后端 app（仅内存，不监听端口）…", flush=True)
from backend.app import main as main_module  # noqa: E402
from backend.app import pipeline as pipeline_module  # noqa: E402

app = main_module.app

# --- 关掉耗资源/有副作用的启动副作用（预热 ASR、托管 shim）--------------
os.environ["CLOUD_STACK_ASR_PREWARM"] = "0"
os.environ["CLOUD_STACK_SHIM_AUTOSTART"] = "0"

# --- 拦截 run_async：只记录，不执行 -------------------------------------
QUEUED: list[dict] = []
store = pipeline_module.store
original_run_async = store.run_async


def fake_run_async(job, **kwargs):
    QUEUED.append(dict(getattr(job, "request", {}) or {}))
    return None


store.run_async = fake_run_async  # type: ignore[assignment]

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)

# 本地模式：.env 里 AUTH_MODE=local ⇒ require_user 恒通过
section("0 前置条件")
from backend.app.auth import local_auth_enabled  # noqa: E402

check("本地鉴权模式已启用（无需登录即可提交）", local_auth_enabled() is True)

BASE = {
    "project_name": "mimo_e2e_probe",
    "script": "这是一段用于端到端验证的测试文案，长度足够通过最小字数校验。",
    "module1_only": True,          # 只走模块 1，避开语言/图像 Key 门禁
    "tts_voice_id": "voice_05.wav",
}


def submit(engine: str) -> tuple[int, dict]:
    payload = dict(BASE)
    payload["tts_engine"] = engine
    response = client.post("/api/jobs", json=payload)
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = {"raw": response.text[:200]}
    return response.status_code, body


# ===========================================================================
section("1 mimo 能被接受（Literal 扩展 + 路由 adapter 已生效）")
QUEUED.clear()
status, body = submit("mimo")
check("POST /api/jobs tts_engine=mimo 返回 200", status == 200,
      f"status={status} detail={str(body.get('detail'))[:120]}")
check("确实建了 1 个任务", len(QUEUED) == 1, f"{len(QUEUED)}")

mimo_request = QUEUED[-1] if QUEUED else {}
# 归一化发生在 run_pipeline 入口（而不是建任务时）—— 本测试用桩替换了
# run_async，所以流水线没跑。这里**直接调真实的包装层**来验证归一化，
# 走的是它的惰性原函数缝合点（wrapper._cloud_stack_original）。
from plugins.cloud_free_stack.ocv_cloud_stack import mimo_engine  # noqa: E402

run_pipeline_wrapper = pipeline_module.run_pipeline
check("run_pipeline 已被 MiMo 包装层接管",
      getattr(run_pipeline_wrapper, "_cloud_stack_mimo_wrapped", False) is True)


class _Job:
    def __init__(self, request: dict) -> None:
        self.request = request


# 让包装层的「下游」变成空操作，只观察它改了 request
saved_original = run_pipeline_wrapper._cloud_stack_original  # noqa: SLF001
run_pipeline_wrapper._cloud_stack_original = lambda job, store, **kw: None  # type: ignore[attr-defined]  # noqa: SLF001
try:
    probe_job = _Job({"tts_engine": "mimo", "project_name": "p"})
    run_pipeline_wrapper(probe_job, None)
    check(
        "run_pipeline 入口把 tts_engine 归一化为 qwen（argv 才能过 argparse）",
        probe_job.request.get("tts_engine") == "qwen",
        str(probe_job.request.get("tts_engine")),
    )
    check(
        "原引擎值留在 marker 键上",
        probe_job.request.get(mimo_engine.MARKER_KEY) == "mimo",
        str(probe_job.request.get(mimo_engine.MARKER_KEY)),
    )
    # 归一化后 wants_mimo 仍为真（后续 run_command 靠它打水印）
    check("归一化后 wants_mimo() 仍为真", mimo_engine.wants_mimo(probe_job) is True)
finally:
    run_pipeline_wrapper._cloud_stack_original = saved_original  # type: ignore[attr-defined]  # noqa: SLF001


# ===========================================================================
section("2 qwen 仍走原生（未被 MiMo 劫持）")
QUEUED.clear()
status, body = submit("qwen")
check("POST /api/jobs tts_engine=qwen 返回 200", status == 200, f"status={status}")
qwen_request = QUEUED[-1] if QUEUED else {}
check("qwen 任务不带 MiMo 标记",
      qwen_request.get("_cloud_stack_tts_engine") is None,
      str(qwen_request.get("_cloud_stack_tts_engine")))
check("qwen 任务 tts_engine 保持 qwen",
      qwen_request.get("tts_engine") == "qwen", str(qwen_request.get("tts_engine")))


# ===========================================================================
section("3 两个选项并存：引擎分发到不同实现")
from plugins.cloud_free_stack.ocv_cloud_stack import mimo_tts  # noqa: E402
from backend.app import qwen_tts as qwen_module  # noqa: E402

mimo_job = _Job(mimo_request)
qwen_job = _Job(qwen_request)

check("mimo 任务 wants_mimo=True", mimo_engine.wants_mimo(mimo_job) is True)
check("qwen 任务 wants_mimo=False", mimo_engine.wants_mimo(qwen_job) is False)

# 模拟子进程分流：给 mimo 任务打水印后，合成实现必须变成 MiMo；
# 而 qwen 任务不打水印，必须保持原生。
original_synth = qwen_module.synthesize_to_file
mimo_engine._RUNTIME_APPLIED = False  # noqa: SLF001
os.environ.pop(mimo_engine.ACTIVE_ENV, None)

# qwen 路径
mimo_engine.apply_child_runtime()
check("qwen 子进程：合成实现仍是 OCV 原生",
      qwen_module.synthesize_to_file is original_synth)

# mimo 路径
mimo_engine._RUNTIME_APPLIED = False  # noqa: SLF001
os.environ[mimo_engine.ACTIVE_ENV] = "1"
mimo_engine.apply_child_runtime()
check("mimo 子进程：合成实现已切到 MiMo",
      qwen_module.synthesize_to_file is mimo_tts.synthesize_to_file)
check("MiMo 实现与原生实现确实是两个不同对象",
      mimo_tts.synthesize_to_file is not original_synth)

# 还原
qwen_module.synthesize_to_file = original_synth
mimo_engine._RUNTIME_APPLIED = False  # noqa: SLF001
os.environ.pop(mimo_engine.ACTIVE_ENV, None)


# ===========================================================================
section("4 run_command 只为「mimo 的配音子进程」打水印")
# 关键：**不替换 run_command 本身**（那会把 MiMo 包装层整个丢掉，本测试
# 第一版就是这么写错的）。改为替换包装层的「下游」缝合点，观测它加了什么 env。
CAPTURED: list[dict] = []
run_command_wrapper = pipeline_module.run_command
check("run_command 已被 MiMo 包装层接管",
      getattr(run_command_wrapper, "_cloud_stack_mimo_wrapped", False) is True)

saved_rc_original = run_command_wrapper._cloud_stack_original  # noqa: SLF001


def fake_downstream(job, store_obj, command, label, **kwargs):
    CAPTURED.append({
        "command": list(command),
        "label": label,
        "extra_env": dict(kwargs.get("extra_env") or {}),
    })
    return None


run_command_wrapper._cloud_stack_original = fake_downstream  # type: ignore[attr-defined]  # noqa: SLF001
try:
    # mimo 任务的配音子进程 → 应有水印
    run_command_wrapper(
        mimo_job, None, [sys.executable, "module1_agent_director.py", "--tts-engine", "qwen"], "l",
    )
    check("mimo 配音子进程带 CLOUD_STACK_MIMO_ACTIVE=1",
          CAPTURED[-1]["extra_env"].get(mimo_engine.ACTIVE_ENV) == "1",
          str(CAPTURED[-1]["extra_env"]))

    # mimo 任务的**非配音**子进程 → 不应有水印
    run_command_wrapper(
        mimo_job, None, [sys.executable, "module5_video_render.py"], "l",
    )
    check("mimo 的非配音子进程不带水印（范围严格受限）",
          mimo_engine.ACTIVE_ENV not in CAPTURED[-1]["extra_env"],
          str(CAPTURED[-1]["extra_env"]))

    # qwen 任务的配音子进程 → 不应有水印
    run_command_wrapper(
        qwen_job, None, [sys.executable, "module1_agent_director.py", "--tts-engine", "qwen"], "l",
    )
    check("qwen 的配音子进程不带水印（原生通道未被污染）",
          mimo_engine.ACTIVE_ENV not in CAPTURED[-1]["extra_env"],
          str(CAPTURED[-1]["extra_env"]))

    # 边界：归一化**之前**（request 里还是 mimo 原值）也该识别出来
    raw_mimo_job = _Job({"tts_engine": "mimo"})
    run_command_wrapper(
        raw_mimo_job, None, [sys.executable, "module1_agent_director.py"], "l",
    )
    check("未归一化的 mimo 任务也能被识别（wants_mimo 兼容原值）",
          CAPTURED[-1]["extra_env"].get(mimo_engine.ACTIVE_ENV) == "1",
          str(CAPTURED[-1]["extra_env"]))
finally:
    run_command_wrapper._cloud_stack_original = saved_rc_original  # type: ignore[attr-defined]  # noqa: SLF001


# ===========================================================================
section("5 清理与还原")
store.run_async = original_run_async  # type: ignore[assignment]
check("已还原 JobStore.run_async", store.run_async == original_run_async)
check("run_command 包装层的下游缝合点已复位",
      run_command_wrapper._cloud_stack_original is saved_rc_original)  # noqa: SLF001

section("汇总")
print(f"  通过 {PASS} / 失败 {FAIL}")
if FAIL:
    print("  [!!] 存在失败项")
    raise SystemExit(1)
print("  全部通过")
raise SystemExit(0)
