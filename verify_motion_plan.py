"""验证动态阶段方案 `reference_beat` 健壮性补丁（F-007）。

## 复现的原始故障

用户报错：``动态阶段方案修订仍未通过：核心参考图对应的阶段编号无效``

根因：``backend/app/video_motion_plan.py::normalize_motion_plan`` 把
``reference_beat``（LLM 产出的**序号**记账字段）当硬契约校验；越界/类型漂移
即抛错，而 ``video_agents.direct_motion`` 只重试一次就升级为**致命错误**。

**同一个字段还被当数组下标用**（``prompt_plan_issues`` 里
``plan['beats'][plan['reference_beat'] - 1]``）⇒ 不修会直接 IndexError。

## 本脚本验四件事

  A. 负向对照：原生实现确实对越界值抛「核心参考图对应的阶段编号无效」
  B. 正向：补丁装上后同一输入通过，且**夹紧到合法区间**
  C. 不误伤：合法值零干预；其它字段仍然严格（不吃掉真错误）
  D. 绑定陷阱：三个 `from ... import` 调用方的命名空间也要换掉
"""
from __future__ import annotations

import copy
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

from ocv_cloud_stack import bootstrap, motion_plan_resilience  # noqa: E402

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


def base_plan(reference_beat, beats=3, version=2):
    """构造一份结构完整、只有 reference_beat 有问题的方案。"""
    return {
        "version": version,
        "scene_anchor": "夜晚的书房，暖光台灯",
        "participants": ["林工"],
        "beats": [
            {"action": f"第{i}阶段：林工做出可见动作", "texts": []}
            for i in range(1, beats + 1)
        ],
        "reference_beat": reference_beat,
        "reference_visual": "林工坐在书桌前翻看资料，桌面有一盏台灯",
        "reference_participants": ["林工"],
        "reference_texts": [],
    }


# 先确保插件注入链已生效（导入补丁目标模块）
import backend.app.video_motion_plan as vmp  # noqa: E402
import backend.app.video_agents as va  # noqa: E402
import backend.app.video_plan as vp  # noqa: E402
import backend.app.video_prompt_refresh as vpr  # noqa: E402

print("== A) 负向对照：原生实现对越界 reference_beat 必须抛错 ==")
# 临时撤掉补丁，拿原生函数做对照
motion_plan_resilience.uninstall()
native = vmp.normalize_motion_plan
check("拿到的是原生实现（无包装标记）",
      not getattr(native, "_cloud_stack_wrapped", False))

for bad, desc in ((0, "0（越界下界）"), (4, "4（越界上界，共3阶段）"),
                  (99, "99（远越界）")):
    raised = ""
    try:
        native(base_plan(bad))
    except ValueError as exc:
        raised = str(exc)
    check(f"原生对 reference_beat={desc} 抛「阶段编号无效」",
          raised == "核心参考图对应的阶段编号无效", raised or "（没有抛错）")

for bad, desc in (("1", "字符串 '1'"), (None, "null"), ("x", "不可解析字符串")):
    raised = ""
    try:
        native(base_plan(bad))
    except ValueError as exc:
        raised = str(exc)
    check(f"原生对 reference_beat={desc} 也抛错（类型漂移）",
          raised == "核心参考图对应的阶段编号无效", raised or "（没有抛错）")

check("原生对合法值 1/2/3 通过", all(native(base_plan(v)) for v in (1, 2, 3)))

print("\n== B) 正向：补丁装上后同一输入通过，并夹紧到合法区间 ==")
motion_plan_resilience.install(vmp, va, vp, vpr)
patched = vmp.normalize_motion_plan
check("补丁已生效（有包装标记）", getattr(patched, "_cloud_stack_wrapped", False))

cases = [
    (0, 1, "0 -> 1"),
    (4, 3, "4 -> 3（共3阶段）"),
    (99, 3, "99 -> 3"),
    (2, 2, "2 合法，原样"),
    ("2", 2, "字符串 '2' -> 2"),
    (2.0, 2, "浮点 2.0 -> 2"),
    (None, 1, "null -> 1（契约默认）"),
    ("x", 1, "不可解析 -> 1"),
]
for raw, expected, desc in cases:
    try:
        got = patched(base_plan(raw))
        check(f"reference_beat {desc}", got["reference_beat"] == expected,
              f"得到 {got['reference_beat']}")
    except Exception as exc:  # noqa: BLE001
        check(f"reference_beat {desc}", False, f"{type(exc).__name__}: {exc}")

print("\n== B2) 关键：不再 IndexError（该字段被当数组下标用）==")
for raw in (0, 99, None, "x"):
    plan = patched(base_plan(raw))
    try:
        # 这正是 prompt_plan_issues 内部会做的下标访问
        texts = plan["beats"][plan["reference_beat"] - 1]["texts"]
        ok = True
        detail = f"reference_beat={plan['reference_beat']} 下标可用"
    except (IndexError, KeyError, TypeError) as exc:
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    check(f"原始 {raw!r} 修复后下标访问安全", ok, detail)

print("\n== C) 不误伤：语义/结构类仍严格；记账类已改由本补丁修复 ==")
#
# ⚠ 本节的期望在 1.8.2 更新过：原先列在"严格"里的三条
#   （主体名重复 / 文字归属未登记 / 核心图主体非子集）已被
#   ``repair_participants`` / ``repair_texts`` **有意修好** ——
#   它们是记账/格式问题，不是语义问题（见 F-009 的分类原则）。
#   留在这里会与新行为矛盾，因此改为断言"**已修复**"，而真正的
#   语义/结构类（version / 阶段数 / 文案为空）继续断言"严格拒绝"。
strict_cases = [
    ("版本非法", {**base_plan(1), "version": 9}, "动态阶段方案版本或格式无效"),
    ("阶段数为 0", {**base_plan(1), "beats": []}, "动态阶段方案需要1～6个连续阶段"),
    ("阶段数超 6", {**base_plan(1), "beats": [{"action": "x", "texts": []}] * 7},
     "动态阶段方案需要1～6个连续阶段"),
]
for desc, plan, expected in strict_cases:
    raised = ""
    try:
        patched(plan)
    except ValueError as exc:
        raised = str(exc)
    check(f"仍严格拒绝：{desc}", raised == expected, raised or "（竟然通过了）")

# 记账/格式类：现在应被**修好**（这是 F-009 的修复目标）
fixed_cases = [
    ("主体名重复", {**base_plan(1), "participants": ["A", "A"]}),
    ("文字归属不在主体列表",
     {**base_plan(1), "beats": [{"action": "x", "texts": [
         {"text": "嗨", "owner": "不存在的人", "container": "对话气泡"}]}] * 3}),
    ("核心图主体不是 participants 子集",
     {**base_plan(1), "participants": ["A"], "reference_participants": ["A", "没登记过的人"]}),
]
for desc, plan in fixed_cases:
    raised = ""
    out = None
    try:
        out = patched(copy.deepcopy(plan))
    except ValueError as exc:
        raised = str(exc)
    check(f"记账类已修复（不再是致命错误）：{desc}", out is not None and not raised,
          raised or f"participants={out['participants'] if out else '?'}")

# 修法是「补登记」而不是「删主体」—— 不能静默丢人
out = patched({**base_plan(1), "participants": ["A"], "reference_participants": ["A", "助手"]})
check("子集违约靠补登记解决，核心图主体未被删",
      out["reference_participants"] == ["A", "助手"] and "助手" in out["participants"],
      f"refs={out['reference_participants']} participants={out['participants']}")

# 合法方案零干预（逐字节相同）
legal = base_plan(2)
out = patched(copy.deepcopy(legal))
check("合法值零干预（数值不变）", out["reference_beat"] == 2)
check("合法方案其它字段原样",
      out["scene_anchor"] == legal["scene_anchor"] and out["participants"] == legal["participants"])

print("\n== D) 绑定陷阱：三个 from-import 调用方也必须被换掉 ==")
for name, module in (("video_agents", va), ("video_plan", vp),
                     ("video_prompt_refresh", vpr)):
    fn = getattr(module, "normalize_motion_plan", None)
    check(f"{name} 命名空间里是包装版（否则补丁形同不存在）",
          getattr(fn, "_cloud_stack_wrapped", False),
          type(fn).__name__)

# 真正走一遍调用方：video_agents 里的下标访问点
check("video_agents 手里的函数能修越界值",
      va.normalize_motion_plan(base_plan(99))["reference_beat"] == 3)
check("video_plan 手里的函数能修越界值",
      vp.normalize_motion_plan(base_plan(0))["reference_beat"] == 1)

print("\n== E) 开关与诊断 ==")
import os  # noqa: E402

check("默认开启", motion_plan_resilience.enabled() is True)
prev = os.environ.get("CLOUD_STACK_MOTION_PLAN_RESILIENT")
try:
    os.environ["CLOUD_STACK_MOTION_PLAN_RESILIENT"] = "0"
    check("置 0 可关闭（回退原生行为）", motion_plan_resilience.enabled() is False)
    raised = ""
    try:
        vmp.normalize_motion_plan(base_plan(99))
    except ValueError as exc:
        raised = str(exc)
    check("关闭后越界值重新报原生错", raised == "核心参考图对应的阶段编号无效",
          raised or "（没有抛错）")
finally:
    if prev is None:
        os.environ.pop("CLOUD_STACK_MOTION_PLAN_RESILIENT", None)
    else:
        os.environ["CLOUD_STACK_MOTION_PLAN_RESILIENT"] = prev
check("恢复后重新生效", motion_plan_resilience.enabled() is True)

diag = motion_plan_resilience.diagnostics()
print(f"    诊断：{diag['repaired_count']} 次修复，包装模块 {diag['wrapped_modules']}")
check("诊断记录了修复次数（不静默）", diag["repaired_count"] > 0)
check("诊断列出被包装的模块", len(diag["wrapped_modules"]) >= 2,
      str(diag["wrapped_modules"]))

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
