"""端到端验证：走 OCV **真实的** direct_motion 调用链，确认不再终止动态视频生产。

前一个脚本（`verify_motion_plan.py`）验的是「夹紧逻辑对不对」；
本脚本验的是**用户实际遇到的那条路**：

    video_agents.direct_motion(...)
      -> ask() 返回带越界 reference_beat 的方案
      -> restore_reference_draft() -> normalize_motion_plan()
      -> 原生：抛「核心参考图对应的阶段编号无效」
      -> direct_motion 第二次仍失败 => 抛
         「动态阶段方案修订仍未通过：核心参考图对应的阶段编号无效」  ← 用户看到的

本脚本用一个**假的 ask**（不调真模型）稳定复现上述路径：

  A. 负向对照：关掉补丁 ⇒ 必须复现出**与用户逐字相同**的那条报错
  B. 正向：开着补丁 ⇒ 同一输入通过，且返回的 reference_beat 在合法区间
  C. 不误伤：真实合法的 LLM 输出不该被改动（数值原样）
"""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = PROJECT_ROOT / "plugins" / "cloud_free_stack"
for entry in (str(PROJECT_ROOT), str(PLUGIN_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

try:
    from ocv_cloud_stack import console as _console

    _console.harden()
except Exception:  # noqa: BLE001
    pass

from ocv_cloud_stack import motion_plan_resilience  # noqa: E402

import backend.app.video_agents as va  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  {detail}" if detail else ""))


# ---- 构造 OCV 期望的输入 -------------------------------------------------

CONTEXT = {
    "video_direction": {"dynamic_text_mode": "text_assisted"},
    "story_context": "示例上下文",
}

SHOT = {
    "id": "shot_001",
    "kind": "video",
    "duration": 6,
    "generation_duration": 6,
    "visual_description": "林工坐在书桌前翻看资料，桌上有一盏暖光台灯。",
    "intent": "说明复核账目的过程",
    "source_subtitles": ["他坐下来复核了一遍账目。"],
    "reference_ids": [],
    "numbered_references": [{"number": "图1", "purpose": "本镜头核心分镜图"}],
}


def plan_with_beat(reference_beat, beats=3):
    return {
        "version": 2,
        "scene_anchor": "夜晚书房，暖光台灯，木质书桌",
        "participants": ["林工"],
        "beats": [
            {"action": f"第{i}阶段：林工翻动资料，眉头逐渐舒展", "texts": []}
            for i in range(1, beats + 1)
        ],
        "reference_beat": reference_beat,
        "reference_visual": "林工坐在书桌前翻看资料，桌面摊开一叠单据，台灯亮着",
        "reference_participants": ["林工"],
        "reference_texts": [],
    }


def make_ask(reference_beat):
    """假 ask：稳定返回带指定 reference_beat 的方案（不调真模型）。"""
    calls = {"n": 0}

    def ask(system, payload):
        calls["n"] += 1
        return {"shots": [{"id": "shot_001", "motion_plan": plan_with_beat(reference_beat)}]}

    ask.calls = calls
    return ask


def run_direct_motion(reference_beat):
    """跑真实的 direct_motion，返回 (成功?, 结果或错误文本, ask 调用次数)。"""
    ask = make_ask(reference_beat)
    try:
        rows = va.direct_motion(CONTEXT, [copy.deepcopy(SHOT)], [], ask=ask)
        return True, rows, ask.calls["n"]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", ask.calls["n"]


print("== A) 负向对照：关掉补丁，必须复现用户的原始报错 ==")
motion_plan_resilience.uninstall(va)
os.environ["CLOUD_STACK_MOTION_PLAN_RESILIENCE_DISABLED_PROBE"] = "1"
prev = os.environ.get("CLOUD_STACK_MOTION_PLAN_RESILIENT")
os.environ["CLOUD_STACK_MOTION_PLAN_RESILIENT"] = "0"
try:
    ok, result, calls = run_direct_motion(99)
    check("越界 reference_beat 会失败（复现故障）", not ok, str(result)[:160])
    expected_tail = "动态阶段方案修订仍未通过：核心参考图对应的阶段编号无效"
    check("报错与用户原文逐字一致",
          isinstance(result, str) and result.endswith(expected_tail),
          repr(result)[:200])
    check("确实重试过一次后才升级为致命错误（attempt 0 + 1）", calls == 2, f"ask 调了 {calls} 次")
finally:
    if prev is None:
        os.environ.pop("CLOUD_STACK_MOTION_PLAN_RESILIENT", None)
    else:
        os.environ["CLOUD_STACK_MOTION_PLAN_RESILIENT"] = prev
    os.environ.pop("CLOUD_STACK_MOTION_PLAN_RESILIENCE_DISABLED_PROBE", None)

print("\n== B) 正向：开着补丁，同一输入必须通过 ==")
motion_plan_resilience.install(va)
for raw, expected in ((99, 3), (0, 1), (4, 3), ("2", 2), (None, 1)):
    ok, result, calls = run_direct_motion(raw)
    if not ok:
        check(f"reference_beat={raw!r} 通过（不再终止任务）", False, str(result)[:160])
        continue
    got = result[0]["motion_plan"]["reference_beat"]
    check(f"reference_beat={raw!r} 通过且夹紧为 {expected}", got == expected, f"得到 {got}")
    check(f"reference_beat={raw!r} 产出完整 action",
          bool(str(result[0].get("action") or "").strip()),
          f"{len(str(result[0].get('action') or ''))} 字符")

print("\n== B2) 修复后下游不再 IndexError（prompt_plan_issues 会下标访问）==")
from backend.app.video_motion_plan import prompt_plan_issues  # noqa: E402

for raw in (99, 0, None):
    ok, result, _ = run_direct_motion(raw)
    if not ok:
        check(f"raw={raw!r} 能进到下游", False, str(result)[:120])
        continue
    plan = result[0]["motion_plan"]
    try:
        issues = prompt_plan_issues(plan["reference_visual"], plan, "image")
        check(f"raw={raw!r} 修复后 prompt_plan_issues 不抛异常",
              isinstance(issues, list), f"{len(issues)} 条交接提示")
    except Exception as exc:  # noqa: BLE001
        check(f"raw={raw!r} 修复后 prompt_plan_issues 不抛异常", False,
              f"{type(exc).__name__}: {exc}")

print("\n== C) 不误伤：合法方案零改动 ==")
for legal in (1, 2, 3):
    ok, result, _ = run_direct_motion(legal)
    if not ok:
        check(f"合法 reference_beat={legal} 正常通过", False, str(result)[:150])
        continue
    got = result[0]["motion_plan"]["reference_beat"]
    check(f"合法 reference_beat={legal} 数值原样（零干预）", got == legal, f"得到 {got}")

# 阶段数与内容都不该被改动
ok, result, _ = run_direct_motion(1)
if ok:
    plan = result[0]["motion_plan"]
    check("阶段数量未被改动", len(plan["beats"]) == 3, f"{len(plan['beats'])} 个阶段")
    check("场景描述未被改动", plan["scene_anchor"] == "夜晚书房，暖光台灯，木质书桌")
    check("主体未被改动", plan["participants"] == ["林工"])

print("\n== D) 补丁确实挂在 video_agents 的命名空间里 ==")
check("video_agents.normalize_motion_plan 是包装版",
      getattr(va.normalize_motion_plan, "_cloud_stack_wrapped", False))

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
