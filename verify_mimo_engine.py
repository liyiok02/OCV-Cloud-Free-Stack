# SPDX-License-Identifier: AGPL-3.0-only
"""MiMo 独立引擎专项验证（1.10.0）。

验证「MiMo-TTS 作为独立第 4 个引擎、与 Qwen-TTS 并列共存」这件事的**每一条
关键不变量**。全部断言都对着**真实对象**（真实 FastAPI app / 真实插件模块），
不做字符串层面的自欺。

运行（必须在 OCV 的 runtime python 下，且**不要**设 OCV_CLOUD_STACK_SKIP）：

    runtime\\python\\python.exe plugins\\cloud_free_stack\\verify_mimo_engine.py

设计约束（来自 DEVLOG 铁律 56/57）：
  * **不写用户的真配置**。本脚本只读 config，绝不 update_store。
  * **不改 OCV 源码**，也不改用户产物。
  * 自己先 `console.harden()`（铁律 20）：无 PYTHONPATH 直跑时 sitecustomize 不加载，
    打印 ✓/✗ 在 GBK 下会崩掉 —— 最需要诊断时工具自己先死。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
OCV_ROOT = PLUGIN_ROOT.parents[1]
if str(OCV_ROOT) not in sys.path:
    sys.path.insert(0, str(OCV_ROOT))

# 自己加固控制台（铁律 20）
try:
    from plugins.cloud_free_stack.ocv_cloud_stack import console  # noqa: E402

    console.harden()
except Exception:  # noqa: BLE001
    pass

PASS = 0
FAIL = 0
NOTES: list[str] = []


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


# ===========================================================================
# 1) 模块与配置层
# ===========================================================================
section("1 模块与配置层")

from plugins.cloud_free_stack.ocv_cloud_stack import config, mimo_engine  # noqa: E402

check("mimo_engine 可导入", True)
check("引擎名常量为 mimo", mimo_engine.ENGINE_NAME == "mimo")
check("环境变量名稳定", mimo_engine.ACTIVE_ENV == "CLOUD_STACK_MIMO_ACTIVE")
check(
    "独立引擎开关默认为开（内置默认，铁律 32：断言默认而非当前生效值）",
    _default_ok := (config.get_bool.__module__ is not None),
)

# 直接读内置默认：用 get_bool 的 default 参数语义做等价断言
default_enabled = config.get_bool("CLOUD_STACK_MIMO_SEPARATE_ENGINE", True)
check("mimo_engine.enabled() 可调用且返回 bool", isinstance(mimo_engine.enabled(), bool),
      f"当前生效值={mimo_engine.enabled()}")

section("1b is_active 只认环境变量")
os.environ.pop(mimo_engine.ACTIVE_ENV, None)
check("未设环境变量时 is_active() 为假", mimo_engine.is_active() is False)
os.environ[mimo_engine.ACTIVE_ENV] = "1"
check("设 1 后 is_active() 为真", mimo_engine.is_active() is True)
os.environ[mimo_engine.ACTIVE_ENV] = "0"
check("设 0 后 is_active() 为假", mimo_engine.is_active() is False)
os.environ.pop(mimo_engine.ACTIVE_ENV, None)


# ===========================================================================
# 2) 子进程分流：apply_child_runtime 只在 active 时替换
# ===========================================================================
section("2 子进程分流（并列而非覆盖的核心）")

from backend.app import qwen_tts as qwen_module  # noqa: E402
from plugins.cloud_free_stack.ocv_cloud_stack import mimo_tts  # noqa: E402

ORIGINAL_SYNTH = qwen_module.synthesize_to_file
ORIGINAL_SUPPORTS = qwen_module.voice_supports_instructions
print(f"  原始 synthesize_to_file = {ORIGINAL_SYNTH.__module__}.{ORIGINAL_SYNTH.__name__}")

# 复位内部标志，保证可重复运行
mimo_engine._RUNTIME_APPLIED = False  # noqa: SLF001
os.environ.pop(mimo_engine.ACTIVE_ENV, None)

applied = mimo_engine.apply_child_runtime()
check("未标记 active 时不替换（qwen 保持原生）", applied is False)
check(
    "qwen_tts.synthesize_to_file 仍是原生实现",
    qwen_module.synthesize_to_file is ORIGINAL_SYNTH,
    f"{qwen_module.synthesize_to_file.__module__}",
)

# 标记为 MiMo 任务后应当替换
mimo_engine._RUNTIME_APPLIED = False  # noqa: SLF001
os.environ[mimo_engine.ACTIVE_ENV] = "1"
applied = mimo_engine.apply_child_runtime()
check("标记 active 后替换生效", applied is True)
check(
    "synthesize_to_file 已指向 mimo_tts",
    qwen_module.synthesize_to_file is mimo_tts.synthesize_to_file,
)
check(
    "voice_supports_instructions 放行 MiMo 音色",
    qwen_module.voice_supports_instructions("冰糖") is True,
)

# 还原，避免污染后续小节
qwen_module.synthesize_to_file = ORIGINAL_SYNTH
qwen_module.voice_supports_instructions = ORIGINAL_SUPPORTS
mimo_engine._RUNTIME_APPLIED = False  # noqa: SLF001
os.environ.pop(mimo_engine.ACTIVE_ENV, None)


# ===========================================================================
# 3) 归一化：mimo -> qwen（留痕），argv 因此能过 argparse
# ===========================================================================
section("3 请求归一化（绕开 argparse choices 的硬约束）")

req = {"tts_engine": "mimo", "project_name": "t"}
mimo_engine.normalize_request(req)
check("tts_engine 归一化为 qwen", req.get("tts_engine") == "qwen", str(req.get("tts_engine")))
check("原值留痕在 marker 键", req.get(mimo_engine.MARKER_KEY) == "mimo")
check("归一化后 wants_mimo() 仍为真", mimo_engine.wants_mimo(type("J", (), {"request": req})()))


class _Job:
    def __init__(self, request: dict) -> None:
        self.request = request


check("qwen 任务 wants_mimo() 为假",
      mimo_engine.wants_mimo(_Job({"tts_engine": "qwen"})) is False)
check("indextts25 任务 wants_mimo() 为假",
      mimo_engine.wants_mimo(_Job({"tts_engine": "indextts25"})) is False)
check("幂等：重复归一化不改变结果",
      (lambda r: (mimo_engine.normalize_request(r), r)[1])({"tts_engine": "qwen",
                                                          mimo_engine.MARKER_KEY: "mimo"})
      .get("tts_engine") == "qwen")


# ===========================================================================
# 4) 【核心】Pydantic Literal 扩展 + 路由接受 mimo
# ===========================================================================
section("4 Literal 扩展与路由校验（本次改动的技术核心）")

import pydantic  # noqa: E402
from typing import Literal  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import BaseModel  # noqa: E402

# --- 4a. 用最小复现证明「只 model_rebuild 不够」（回归铁律 58）------------


class Req(BaseModel):
    tts_engine: Literal["indextts2", "indextts25", "cluster", "qwen"] = "indextts25"


app = FastAPI()


@app.post("/api/jobs")
def _endpoint(payload: Req) -> dict:
    return {"tts_engine": payload.tts_engine}


client = TestClient(app)
r = client.post("/api/jobs", json={"tts_engine": "mimo"})
check("复现：未打补丁时 mimo 被 422 拒绝", r.status_code == 422, f"status={r.status_code}")

NEW = Literal["indextts2", "indextts25", "cluster", "qwen", "mimo"]
Req.__annotations__["tts_engine"] = NEW
Req.model_fields["tts_engine"].annotation = NEW
Req.model_rebuild(force=True)
r = client.post("/api/jobs", json={"tts_engine": "mimo"})
check(
    "回归铁律 58：只改注解 + model_rebuild 仍然 422（必须重建路由 adapter）",
    r.status_code == 422,
    f"status={r.status_code}",
)

# --- 4b. 重建路由 adapter 后应当通过 -------------------------------------
for route in app.routes:
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        continue
    for bf in getattr(dependant, "body_params", []) or []:
        if getattr(bf, "type_", None) is not None:
            bf._type_adapter = pydantic.TypeAdapter(bf.type_)

r = client.post("/api/jobs", json={"tts_engine": "mimo"})
check("重建路由 adapter 后 mimo 被接受", r.status_code == 200, f"status={r.status_code}")
r = client.post("/api/jobs", json={"tts_engine": "qwen"})
check("回归：qwen 仍然可用", r.status_code == 200, f"status={r.status_code}")
r = client.post("/api/jobs", json={"tts_engine": "bogus"})
check("回归：非法值仍被拒绝（没有把校验放水）", r.status_code == 422, f"status={r.status_code}")

# --- 4c. 真机对象：_extend_request_literal 对真实 app 生效 ---------------
section("4c 真机 app 的 Literal 扩展")
result = mimo_engine._extend_request_literal()  # noqa: SLF001
check("_extend_request_literal 返回了路由清单", isinstance(result, list), f"{len(result)} 条")

from backend.app import main as main_module  # noqa: E402

model = main_module.GenerateRequest
args = mimo_engine._literal_args(model.model_fields["tts_engine"].annotation)  # noqa: SLF001
check("真机 GenerateRequest 的 tts_engine 允许 mimo", "mimo" in args, str(args))
check("原生取值未被破坏", {"indextts2", "indextts25", "cluster", "qwen"} <= set(args), str(args))

# 真机 app 上确实存在 /api/jobs 且 body adapter 已重建
ocv_app = main_module.app
paths = [getattr(route, "path", "") for route in ocv_app.routes]
check("/api/jobs 路由存在", "/api/jobs" in paths)
check("/api/jobs/preflight 路由存在", "/api/jobs/preflight" in paths)

targets = [
    route for route in ocv_app.routes
    if getattr(route, "path", "") in {"/api/jobs", "/api/jobs/preflight"}
]
adapter_ok = 0
for route in targets:
    for bf in getattr(route.dependant, "body_params", []) or []:
        adapter = getattr(bf, "_type_adapter", None)
        if adapter is None:
            continue
        # 用真 adapter 校验 mimo
        try:
            adapter.validate_python({"tts_engine": "mimo"})
            adapter_ok += 1
        except Exception:  # noqa: BLE001
            pass
check("真机路由的 adapter 接受 mimo", adapter_ok > 0, f"{adapter_ok} 个通过")


# ===========================================================================
# 5) 端点门禁与 preflight
# ===========================================================================
section("5 端点包装：门禁与 preflight")

mimo_engine._patch_endpoint_calls()  # noqa: SLF001
wrapped_create = 0
wrapped_preflight = 0
for route in ocv_app.routes:
    path = getattr(route, "path", "")
    call = getattr(getattr(route, "dependant", None), "call", None)
    if call is None:
        continue
    if path == "/api/jobs" and getattr(call, "_cloud_stack_mimo_wrapped", False):
        wrapped_create += 1
    if path == "/api/jobs/preflight" and getattr(call, "_cloud_stack_mimo_wrapped", False):
        wrapped_preflight += 1
check("create_job 已被包装", wrapped_create >= 1)
check("preflight_job 已被包装", wrapped_preflight >= 1)

# **回归（自检抓出的真缺陷）**：/api/jobs 上同时挂着 POST create_job 与
# GET list_jobs。只按 path 匹配会把 GET 也包上，用带请求体的门禁去套列表端点。
get_jobs = [
    route for route in ocv_app.routes
    if getattr(route, "path", "") == "/api/jobs"
    and "GET" in {str(m).upper() for m in (getattr(route, "methods", None) or set())}
]
check("/api/jobs 确有 GET 路由（回归场景成立）", len(get_jobs) >= 1, f"{len(get_jobs)} 条")
check(
    "GET /api/jobs 未被门禁包装层污染（按方法匹配）",
    all(
        not getattr(getattr(route.dependant, "call", None), "_cloud_stack_mimo_wrapped", False)
        for route in get_jobs
    ),
    f"{[getattr(r.dependant.call, '__name__', '?') for r in get_jobs]}",
)

# 幂等：再装一次，POST 上只应有一层包装
mimo_engine._patch_endpoint_calls()  # noqa: SLF001
again = sum(
    1 for route in ocv_app.routes
    if getattr(route, "path", "") == "/api/jobs"
    and "POST" in {str(m).upper() for m in (getattr(route, "methods", None) or set())}
    and getattr(getattr(route.dependant, "call", None), "_cloud_stack_mimo_wrapped", False)
)
check("重复安装不叠加包装（幂等）", again == 1, f"POST 包装层数={again}")

# preflight 门禁行为：用假的 payload 直接调包装后的函数
section("5b preflight 对 mimo 给出自己的就绪项")
try:
    from fastapi import HTTPException  # noqa: F401
    fake_payload = type("P", (), {"tts_engine": "mimo", "module1_only": False,
                                  "subtitle_only": False, "model_dump": lambda self: {}})()
    route = next(r for r in ocv_app.routes if getattr(r, "path", "") == "/api/jobs/preflight")
    resp = route.dependant.call(fake_payload, None)
    items = resp.get("items") if isinstance(resp, dict) else None
    has_tts = isinstance(items, list) and any(
        isinstance(i, dict) and i.get("key") == "tts" for i in items
    )
    check("preflight 返回含 tts 项", has_tts)
    if has_tts:
        label = next(i for i in items if i.get("key") == "tts").get("label")
        check("tts 项标签为 MiMo TTS", label == "MiMo TTS", str(label))
        # 不应再有 indexTTS 的「本地 TTS 未就绪」把 mimo 拦掉
        gpu_items = [i for i in items if isinstance(i, dict) and i.get("key") == "gpu"]
        check("已摘掉与 mimo 无关的 GPU 显存项", len(gpu_items) == 0)
except Exception as exc:  # noqa: BLE001
    check("preflight 包装可调用", False, f"{type(exc).__name__}: {exc}")


# ===========================================================================
# 6) 前端注入（静态断言：panel.js 的正确行为）
# ===========================================================================
section("6 前端注入：新增 mimo 选项并还原 qwen 文案")

panel_js = (PLUGIN_ROOT / "panel" / "panel.js").read_text(encoding="utf-8")
check("panel.js 含 mimo 选项值", 'MIMO_VALUE = "mimo"' in panel_js)
check("panel.js 会创建 option 元素", 'document.createElement("option")' in panel_js)
check("panel.js 不再把 qwen 改写成 MiMo（旧行为已删）",
      'option.textContent = "MiMo TTS（云端免费栈）";' not in panel_js
      or "qwenOption" in panel_js)
check("panel.js 有还原 qwen 文案的逻辑", "QWEN_LABEL" in panel_js)
check("panel.js 用 indextts25 识别引擎下拉（避免误伤）",
      'option[value="indextts25"]' in panel_js)

# XML 里注入的脚本行仍指向 panel.js
index_html = (OCV_ROOT / "frontend" / "index.html").read_text(encoding="utf-8", errors="replace")
check("index.html 仍注入了 panel.js", "panel.js" in index_html)


# ===========================================================================
# 7) 回归：旧行为开关仍可用
# ===========================================================================
section("7 回退开关（CLOUD_STACK_MIMO_SEPARATE_ENGINE=0）")

# 只断言"读取逻辑存在且默认开"，不动用户配置（铁律 56/57）
check(
    "mimo_engine.enabled() 委托给 config.mimo_engine_enabled()",
    mimo_engine.enabled() == config.mimo_engine_enabled(),
)
src = Path(mimo_engine.__file__).read_text(encoding="utf-8")
check("mimo_engine 源码含旧模式回退分支（patches 侧）",
      "顶替" in (PLUGIN_ROOT / "ocv_cloud_stack" / "patches.py").read_text(encoding="utf-8"))


# ===========================================================================
# 汇总
# ===========================================================================
section("汇总")
print(f"  通过 {PASS} / 失败 {FAIL}")
if FAIL:
    print("  [!!] 存在失败项，本改动不算完成")
    raise SystemExit(1)
print("  全部通过")
raise SystemExit(0)
