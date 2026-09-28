"""决定性端到端：复刻**日志里那个模型的实际输出形态**，走真实 direct_motion。

## 为什么要这样测

后端日志（runtime_logs/backend.stdout.log）显示该模型**反复**返回：

    reference_beat: None          -> F-007
    reference_visual: ""          -> F-008
    reference_participants: 缺失   -> F-009（本轮用户报错）

这三者是**同一个模型在同一份输出上的系统性缺失**，不是三个独立故障。
前两轮我逐个修，用户就逐个撞到下一个 —— 典型的"打地鼠"。

本脚本模拟的正是这个**组合形态**（三个字段同时坏），验证一次通过：
  A. 三者同时缺失 -> 必须通过（不能再让用户撞第四个）
  B. 三者同时坏且**还叠加**文字/主体漂移 -> 仍须通过
  C. 走 OCV 真实的 direct_motion（含其两次重试逻辑）
  D. 产物必须是一份**结构完整**的方案（下游 prompt_plan_issues 不炸）
"""
from __future__ import annotations

import copy
import json
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

import backend.app.video_agents as va  # noqa: E402
from backend.app.video_motion_plan import prompt_plan_issues  # noqa: E402

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


CONTEXT = {"video_direction": {"dynamic_text_mode": "text_assisted"}, "story_context": "ctx"}
SHOT = {
    "id": "shot_001", "kind": "video", "duration": 6, "generation_duration": 6,
    "visual_description": "夜晚书房，林工坐在书桌前翻看一叠单据，暖光台灯照亮桌面。",
    "intent": "说明复核账目的过程",
    "source_subtitles": ["他坐下来复核了一遍账目。"],
    "reference_ids": [],
    "numbered_references": [{"number": "图1", "purpose": "本镜头核心分镜图"}],
}


def model_like_plan(**overrides):
    """复刻日志里那个模型的输出形态：v2 字段系统性缺失/漂移。"""
    plan = {
        "version": 2,
        "scene_anchor": "夜晚书房，暖光台灯，木质书桌",
        "participants": ["林工"],
        "beats": [
            {"action": "第1阶段：林工翻开单据", "texts": []},
            {"action": "第2阶段：逐页核对，眉头收紧", "texts": []},
            {"action": "第3阶段：眉头舒展，放下单据", "texts": []},
        ],
        # ⚠ 以下三个正是日志里反复出现的问题形态
        "reference_beat": None,
        "reference_visual": "",
        "reference_participants": None,
        "reference_texts": None,
    }
    plan.update(overrides)
    return plan


def run_direct(plan):
    calls = {"n": 0}

    def ask(system, payload):
        calls["n"] += 1
        return {"shots": [{"id": "shot_001", "motion_plan": copy.deepcopy(plan)}]}

    try:
        rows = va.direct_motion(CONTEXT, [copy.deepcopy(SHOT)], [], ask=ask)
        return True, rows, calls["n"]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", calls["n"]


print("== A) 三个 v2 字段同时缺失（日志里的实际形态）==")
ok, result, calls = run_direct(model_like_plan())
check("端到端通过（不再终止任务）", ok, str(result)[:160] if not ok else f"ask 调 {calls} 次")
if ok:
    plan = result[0]["motion_plan"]
    check("reference_beat 已补齐为合法 int",
          type(plan["reference_beat"]) is int and 1 <= plan["reference_beat"] <= 3,
          repr(plan["reference_beat"]))
    check("reference_visual 已填入画面草案", bool(str(plan["reference_visual"]).strip()),
          f"{len(plan['reference_visual'])} 字符")
    check("reference_participants 已填入", bool(plan["reference_participants"]),
          str(plan["reference_participants"]))
    check("只调了一次模型（没有浪费第二次重试）", calls == 1, f"{calls} 次")

print("\n== B) 叠加更多漂移：主体/文字的记账问题一起上 ==")
hard = model_like_plan(
    participants=["林工", "林工", None, "助手"],           # 重名 + 占位符
    beats=[
        {"action": "第1阶段：林工与助手对话", "texts": [
            {"text": "账目", "owner": "助手", "container": "对话气泡"},
            {"text": "账目", "owner": "助手", "container": "对话气泡"}]},   # 重复
        {"action": "第2阶段：核对", "texts": [
            {"text": "逐页核对", "owner": "林工", "container": "容" * 120}]},  # 超长容器
        {"action": "第3阶段：完成", "texts": None},                       # 缺失
    ],
    reference_texts=[{"text": "复核", "owner": "助手", "container": "说明框"}],  # owner 对 refs
)
ok, result, calls = run_direct(hard)
check("叠加漂移后端到端仍通过", ok, str(result)[:200] if not ok else f"ask 调 {calls} 次")
if ok:
    plan = result[0]["motion_plan"]
    check("主体已去重", len(plan["participants"]) == len(set(plan["participants"])),
          str(plan["participants"]))
    check("占位符 null 已丢弃", None not in plan["participants"], str(plan["participants"]))
    check("重复文字记录已去重", len(plan["beats"][0]["texts"]) == 1,
          str(plan["beats"][0]["texts"]))
    check("超长容器已截断", len(plan["beats"][1]["texts"][0]["container"]) <= 80,
          f"{len(plan['beats'][1]['texts'][0]['container'])} 字符")
    check("缺失的 texts 已补成 []", plan["beats"][2]["texts"] == [])
    check("reference_participants ⊆ participants",
          all(n in plan["participants"] for n in plan["reference_participants"]),
          f"{plan['reference_participants']} vs {plan['participants']}")
    check("reference_texts 的 owner 也已在 reference_participants 里",
          all(r["owner"] in plan["reference_participants"] or r["owner"] == "画面标注"
              for r in plan["reference_texts"]),
          str(plan["reference_participants"]))

print("\n== C) 产物必须结构完整（下游 prompt_plan_issues 不炸）==")
ok, result, _ = run_direct(hard)
if ok:
    plan = result[0]["motion_plan"]
    try:
        issues = prompt_plan_issues(plan["reference_visual"], plan, "image")
        check("prompt_plan_issues 不抛异常", isinstance(issues, list),
              f"{len(issues)} 条交接提示")
    except Exception as exc:  # noqa: BLE001
        check("prompt_plan_issues 不抛异常", False, f"{type(exc).__name__}: {exc}")
    check("action 已生成", bool(str(result[0].get("action") or "").strip()),
          f"{len(str(result[0].get('action') or ''))} 字符")

print("\n== D) 二次校验：修完的输出能被原生再次接受（幂等）==")
import backend.app.video_motion_plan as vmp  # noqa: E402

ok, result, _ = run_direct(hard)
if ok:
    plan = result[0]["motion_plan"]
    try:
        again = vmp.normalize_motion_plan(copy.deepcopy(plan))
        check("修后方案过原生二次校验", bool(again))
        check("二次校验不改动内容", json.dumps(again, sort_keys=True, ensure_ascii=False)
              == json.dumps(vmp.normalize_motion_plan(copy.deepcopy(again)),
                            sort_keys=True, ensure_ascii=False))
    except ValueError as exc:
        check("修后方案过原生二次校验", False, str(exc))

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
