"""验证 F-010：`提示词与阶段方案核对失败：<id> 缺少逐阶段定稿段落`。

用户第四轮报错（**六个镜头全部**同一条）：
    提示词与阶段方案核对失败：89faa80923fd 缺少逐阶段定稿段落；
    8cf2a3534acf 缺少逐阶段定稿段落；...（共 6 个）

六个全中 ⇒ 系统性形状漂移：该模型对 Agent 5 的 v2 输出结构
（``continuity_prompt`` + ``beat_prompts[{beat,prompt}]`` + ``ending_prompt``）
系统性不服帖。原生 ``_assemble_video_body`` 有三道硬校验，任一不过即抛错。

## 本脚本验四件事

  A. 负向对照：关掉补丁后，**逐字复现**用户那条报错（含 6 个镜头全中）
  B. 正向：开着补丁，各种形状漂移都被规整通过
  C. **不编造**：连方案 ``action`` 都空时，仍然报错（不产出空段落）
  D. 关键安全性：包装层必须**原地改 row**，否则调用方读到空提示词
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


BEATS = [
    {"action": "第1阶段：林工翻开单据，眉头微皱", "texts": []},
    {"action": "第2阶段：逐页核对，指尖停在某行", "texts": []},
    {"action": "第3阶段：眉头舒展，合上单据", "texts": []},
]


def shot(sid="89faa80923fd"):
    return {
        "id": sid, "kind": "video",
        "motion_plan": {"version": 2, "scene_anchor": "夜晚书房", "participants": ["林工"],
                        "beats": copy.deepcopy(BEATS), "reference_beat": 1,
                        "reference_visual": "林工在书桌前翻单据", "reference_participants": ["林工"],
                        "reference_texts": []},
    }


def row(**kw):
    base = {
        "continuity_prompt": "保持林工的造型、画风与夜晚书房场景",
        "beat_prompts": [
            {"beat": 1, "prompt": "林工翻开单据"},
            {"beat": 2, "prompt": "逐页核对"},
            {"beat": 3, "prompt": "合上单据"},
        ],
        "ending_prompt": "画面停在合上单据的状态，余下时间自然停留",
    }
    base.update(kw)
    return base


print("== A) 负向对照：关掉补丁，逐字复现用户报错 ==")
motion_plan_resilience.uninstall(va)
prev = os.environ.get("CLOUD_STACK_MOTION_PLAN_RESILIENT")
os.environ["CLOUD_STACK_MOTION_PLAN_RESILIENT"] = "0"
try:
    # 复刻用户场景：6 个镜头，全部返回缺 beat_prompts 的行
    ids = ["89faa80923fd", "8cf2a3534acf", "b39a8b6b761e",
           "01fa43df67dd", "5474a27ca715", "cc4ebf852bf8"]
    first_err = None
    for sid in ids:
        try:
            va._assemble_video_body(shot(sid), row(beat_prompts=None))
        except ValueError as exc:
            if first_err is None:
                first_err = str(exc)
    check("原生：beat_prompts=None 报「缺少逐阶段定稿段落」",
          first_err == f"{ids[0]} 缺少逐阶段定稿段落", str(first_err))

    raised = ""
    try:
        va._assemble_video_body(shot(), row(beat_prompts="一段话"))
    except ValueError as exc:
        raised = str(exc)
    check("原生：beat_prompts 是标量也报错",
          raised == f"{ids[0]} 缺少逐阶段定稿段落", raised)

    raised = ""
    try:
        va._assemble_video_body(shot(), row(beat_prompts=["a", "b", "c"]))
    except ValueError as exc:
        raised = str(exc)
    check("原生：字符串列表也报错（这是最可能的真实形态）",
          raised == f"{ids[0]} 缺少逐阶段定稿段落", raised)

    raised = ""
    try:
        va._assemble_video_body(shot(), row(ending_prompt=""))
    except ValueError as exc:
        raised = str(exc)
    check("原生：ending_prompt 为空报「场景、阶段或结尾定稿为空」",
          raised == f"{ids[0]} 的场景、阶段或结尾定稿为空", raised)
finally:
    if prev is None:
        os.environ.pop("CLOUD_STACK_MOTION_PLAN_RESILIENT", None)
    else:
        os.environ["CLOUD_STACK_MOTION_PLAN_RESILIENT"] = prev

print("\n== B) 正向：各种形状漂移都被规整通过 ==")
motion_plan_resilience.install(va)

CASES = [
    ("beat_prompts 为 None（整个缺失 -> 回落 action）", {"beat_prompts": None}, 3),
    ("beat_prompts 是空列表", {"beat_prompts": []}, 3),
    ("beat_prompts 是空字符串", {"beat_prompts": ""}, 3),
    ("beat_prompts 是真标量（无位置可推断，应仍报错）",
     {"beat_prompts": "一段话"}, None),
    ("beat_prompts 是字符串列表（顺序即编号）",
     {"beat_prompts": ["第一段", "第二段", "第三段"]}, 3),
    ("beat 编号从 0 开始", {"beat_prompts": [
        {"beat": 0, "prompt": "a"}, {"beat": 1, "prompt": "b"}, {"beat": 2, "prompt": "c"}]}, 3),
    ("beat 编号重复", {"beat_prompts": [
        {"beat": 1, "prompt": "a"}, {"beat": 1, "prompt": "b"}, {"beat": 1, "prompt": "c"}]}, 3),
    ("beat 编号是字符串", {"beat_prompts": [
        {"beat": "1", "prompt": "a"}, {"beat": "2", "prompt": "b"}, {"beat": "3", "prompt": "c"}]}, 3),
    ("段落数多于阶段数", {"beat_prompts": [
        {"beat": i, "prompt": f"p{i}"} for i in range(1, 6)]}, 3),
    ("段落数少于阶段数", {"beat_prompts": [{"beat": 1, "prompt": "a"}]}, 3),
    ("某阶段正文为空（回落方案 action）",
     {"beat_prompts": [{"beat": 1, "prompt": "a"}, {"beat": 2, "prompt": ""},
                       {"beat": 3, "prompt": "c"}]}, 3),
    ("continuity_prompt 为空（回落 scene_anchor）", {"continuity_prompt": ""}, 3),
    ("continuity_prompt 键缺失", {"continuity_prompt": None}, 3),
    ("ending_prompt 为 None（回落末阶段 action）", {"ending_prompt": None}, 3),
    ("ending_prompt 为空串", {"ending_prompt": ""}, 3),
    ("只有字符串列表且长度不足",
     {"beat_prompts": ["只有一段"]}, 3),
]
for desc, override, expect_parts in CASES:
    r = row(**override)
    try:
        va._assemble_video_body(shot(), r)
        got = r.get("video_prompt") or ""
        n_parts = len([x for x in got.split("\n") if x.strip()])
        if expect_parts is None:
            check(f"真标量不修（应仍报错）：{desc}", False, "竟然通过了")
        else:
            check(f"补丁修好：{desc}", bool(got.strip()) and n_parts >= expect_parts,
                  f"{n_parts} 段落")
    except ValueError as exc:
        if expect_parts is None:
            check(f"真标量仍报错：{desc}", True, str(exc)[:60])
        else:
            check(f"补丁修好：{desc}", False, str(exc))

print("\n== B2) 六个镜头全部一次通过（复刻用户场景）==")
ids = ["89faa80923fd", "8cf2a3534acf", "b39a8b6b761e",
       "01fa43df67dd", "5474a27ca715", "cc4ebf852bf8"]
failed = []
for sid in ids:
    try:
        r = row(beat_prompts=["打开单据", "逐页核对", "合上单据"])
        va._assemble_video_body(shot(sid), r)
        if not (r.get("video_prompt") or "").strip():
            failed.append(f"{sid}: 空提示词")
    except ValueError as exc:
        failed.append(f"{sid}: {exc}")
check("6 个镜头全部通过且都产出非空提示词", not failed, str(failed) if failed else "全部 OK")

print("\n== C) 不编造：连方案 action 都空时仍须报错 ==")
empty_plan = shot()
for beat in empty_plan["motion_plan"]["beats"]:
    beat["action"] = ""
r = row(beat_prompts=[{"beat": 1, "prompt": ""}, {"beat": 2, "prompt": ""},
                      {"beat": 3, "prompt": ""}])
raised = ""
try:
    va._assemble_video_body(empty_plan, r)
except ValueError as exc:
    raised = str(exc)
check("无 action 可回落时仍报错（不产出空段落）", bool(raised), raised or "竟然通过了")

# 部分空：有 action 的回落，没 action 的报错
mixed = shot()
mixed["motion_plan"]["beats"][1]["action"] = ""
r = row(beat_prompts=[{"beat": 1, "prompt": ""}, {"beat": 2, "prompt": ""},
                      {"beat": 3, "prompt": "c"}])
raised = ""
try:
    va._assemble_video_body(mixed, r)
except ValueError as exc:
    raised = str(exc)
check("第 2 阶段无 action 可回落时仍报错", bool(raised), raised or "竟然通过了")

print("\n== D) 关键安全性：包装层必须原地改 row（否则调用方读到空提示词）==")
r = row(beat_prompts=["a", "b", "c"])
before_id = id(r)
va._assemble_video_body(shot(), r)
check("row 对象身份未变（原地修改）", id(r) == before_id)
check("调用方读同一对象能拿到拼好的 video_prompt",
      bool((r.get("video_prompt") or "").strip()),
      f"{len(r.get('video_prompt') or '')} 字符")

print("\n== E) 回落来源必须是**方案自己的 action**（不是编造）==")
plan = shot()
plan["motion_plan"]["beats"][0]["action"] = "【方案原文】林工翻开第一页单据"
r = row(beat_prompts=[{"beat": 1, "prompt": ""},
                      {"beat": 2, "prompt": "b"}, {"beat": 3, "prompt": "c"}])
va._assemble_video_body(plan, r)
out = r["video_prompt"]
check("第 1 阶段回落成了方案原文", "【方案原文】林工翻开第一页单据" in out,
      out.split("\n")[1][:40] if len(out.split("\n")) > 1 else out[:60])

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
