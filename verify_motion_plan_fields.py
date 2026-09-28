"""验证动态阶段方案**整族字段**的健壮性（F-009）。

用户连续三轮撞到同一个 LLM 输出的不同字段：
    F-007  reference_beat   -> 核心参考图对应的阶段编号无效
    F-008  reference_visual -> 参考画面状态为空、格式无效或过长
    F-009  reference_participants -> 核心参考图主体列表无效

逐个打补丁是"打地鼠"。本脚本按**整族字段**验证：一次覆盖
13 个 raise 点里所有「记账/格式类」的，并断言「语义/结构类」仍然严格。

## 分类原则（只修记账/格式，绝不修语义）

| 类别 | 例子 | 处理 |
|---|---|---|
| 形状/重复/超限/类型漂移 | participants 非 list、重名、超 12、owner 未登记 | **修**（意图可还原） |
| 内容/结构缺失 | version 非法、阶段数 0 或 >6、beat 非对象、文字/画面为空 | **严格**（编造会静默产出错数据） |

## 分节

  A. 负向对照：关掉补丁后，整族坏值**全部复现**原生报错
  B. 正向：开着补丁，全部可修项通过，且修法与分类一致
  C. **不误伤**：合法方案零改动；语义/结构类错误**仍然严格拒绝**
  D. 子集契约的修法方向：**补登记**而不是**删主体**（后者会静默丢人）
  E. 上限常量与 OCV 源码一致（引用而非重定义）
"""
from __future__ import annotations

import copy
import os
import re
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

import backend.app.video_motion_plan as vmp  # noqa: E402

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


def base(**overrides):
    plan = {
        "version": 2,
        "scene_anchor": "夜晚书房，暖光台灯，木质书桌",
        "participants": ["林工"],
        "beats": [{"action": f"第{i}阶段：林工翻动资料", "texts": []} for i in range(1, 4)],
        "reference_beat": 1,
        "reference_visual": "林工坐在书桌前翻看单据，台灯亮着",
        "reference_participants": ["林工"],
        "reference_texts": [],
    }
    plan.update(overrides)
    return plan


# (说明, plan, 原生错误片段)
FIXABLE_CASES = [
    ("reference_participants 为 null", base(reference_participants=None), "核心参考图主体列表无效"),
    ("reference_participants 非 list", base(reference_participants="林工"), "核心参考图主体列表无效"),
    ("reference_participants 超 12 个",
     base(reference_participants=[f"人{i}" for i in range(1, 15)]), "核心参考图主体列表无效"),
    ("reference_participants 含占位符 null/{}",
     base(reference_participants=["林工", None, {}, 7]), None),
    ("reference_participants 重名",
     base(reference_participants=["林工", "林工"]), "核心参考图主体必须是本镜主体的不重复子集"),
    ("核心图主体未登记进 participants（子集违约）",
     base(participants=["林工"], reference_participants=["林工", "助手"]),
     "核心参考图主体必须是本镜主体的不重复子集"),
    ("participants 非 list",
     base(participants="林工"), "动态阶段方案主体列表无效"),
    ("participants 是标量字符串（模型把数组写成标量）",
     base(participants="林工、助手"), "动态阶段方案主体列表无效"),
    ("participants 超 12 个",
     base(participants=[f"人{i}" for i in range(1, 15)]), "动态阶段方案主体列表无效"),
    ("participants 重名", base(participants=["林工", "林工"]), "动态阶段方案主体名称重复"),
    ("participants 含 null", base(participants=["林工", None]),
     "动态阶段方案的主体名称为空、格式无效或过长"),
    ("reference_participants 缺失（模型整个不填）",
     base(reference_participants=None), "核心参考图主体列表无效"),
    ("文字 owner 未登记在 participants",
     base(beats=[{"action": "x", "texts": [
         {"text": "账目", "owner": "助手", "container": "对话气泡"}]}] * 3),
     "动态阶段文字归属不在主体列表中"),
    ("文字条目重复",
     base(beats=[{"action": "x", "texts": [
         {"text": "账目", "owner": "林工", "container": "对话气泡"},
         {"text": "账目", "owner": "林工", "container": "对话气泡"}]}] * 3),
     "动态阶段的文字记录重复"),
    ("文字条目非对象",
     base(beats=[{"action": "x", "texts": [None, "垃圾"]}] * 3), "动态阶段文字格式无效"),
    ("文字容器超 80 字符",
     base(beats=[{"action": "x", "texts": [
         {"text": "账目", "owner": "林工", "container": "容" * 120}]}] * 3),
     "动态阶段方案的文字容器为空、格式无效或过长"),
    ("核心图文字 owner 未登记",
     base(reference_texts=[{"text": "账目", "owner": "助手", "container": "对话气泡"}]),
     "核心参考图文字归属不在主体列表中"),
    ("participants 只有空字符串（占位符；契约允许无主体为空）",
     base(participants=[""]), "动态阶段方案的主体名称为空、格式无效或过长"),
    ("reference_texts 缺失", base(reference_texts=None),
     "核心参考图的短文字列表无效"),
]

STRICT_CASES = [
    ("version 非法", base(version=9), "动态阶段方案版本或格式无效"),
    ("阶段数为 0", base(beats=[]), "动态阶段方案需要1～6个连续阶段"),
    ("阶段数超 6", base(beats=[{"action": "x", "texts": []}] * 7),
     "动态阶段方案需要1～6个连续阶段"),
    ("beat 非对象", base(beats=["x"] * 3), "动态阶段格式无效"),
    ("scene_anchor 为空", base(scene_anchor=""), "动态阶段方案的场景基础为空、格式无效或过长"),
    ("stage action 为空", base(beats=[{"action": "", "texts": []}] * 3),
     "动态阶段方案的可见变化为空、格式无效或过长"),
]


def run(plan):
    try:
        return True, vmp.normalize_motion_plan(copy.deepcopy(plan))
    except ValueError as exc:
        return False, str(exc)


print("== A) 负向对照：关掉补丁，整族坏值全部复现原生报错 ==")
motion_plan_resilience.uninstall(vmp)
prev = os.environ.get("CLOUD_STACK_MOTION_PLAN_RESILIENT")
os.environ["CLOUD_STACK_MOTION_PLAN_RESILIENT"] = "0"
try:
    for desc, plan, expected in FIXABLE_CASES:
        ok, got = run(plan)
        if expected is None:
            # 这类在原生下"碰巧"可能通过（占位符被 string() 拦下）——只报告不强断言
            print(f"    · 原生 {desc} -> {'通过' if ok else got}")
        else:
            check(f"原生复现：{desc}", (not ok) and got == expected,
                  got if not ok else "竟然通过")
    for desc, plan, expected in STRICT_CASES:
        ok, got = run(plan)
        check(f"原生严格：{desc}", (not ok) and got == expected, got if not ok else "竟然通过")
finally:
    if prev is None:
        os.environ.pop("CLOUD_STACK_MOTION_PLAN_RESILIENT", None)
    else:
        os.environ["CLOUD_STACK_MOTION_PLAN_RESILIENT"] = prev

print("\n== B) 正向：开着补丁，可修项必须全部通过 ==")
motion_plan_resilience.install(vmp)
for desc, plan, _expected in FIXABLE_CASES:
    ok, got = run(plan)
    check(f"补丁修好：{desc}", ok, got if not ok else f"{len(str(got))} 字段")
    if ok:
        # 修完的输出必须仍然是一份**结构合法**的方案（过原生二次校验）
        try:
            vmp.normalize_motion_plan(got)
        except ValueError as exc:
            check(f"  修后仍合法：{desc}", False, str(exc))

print("\n== C) 不误伤：语义/结构类**仍然严格拒绝** ==")
for desc, plan, expected in STRICT_CASES:
    ok, got = run(plan)
    check(f"补丁仍严格拒绝：{desc}", (not ok) and got == expected,
          got if not ok else "竟然通过了（不该修！）")

print("\n== C2) 合法方案零改动 ==")
legal = base()
ok, got = run(legal)
check("合法方案通过", ok, str(got)[:60] if not ok else "")
if ok:
    check("participants 未变", got["participants"] == legal["participants"], str(got["participants"]))
    check("reference_participants 未变",
          got["reference_participants"] == legal["reference_participants"],
          str(got["reference_participants"]))
    check("reference_beat 未变", got["reference_beat"] == 1)
    check("reference_visual 未变", got["reference_visual"] == legal["reference_visual"])
    check("阶段数未变", len(got["beats"]) == 3)

print("\n== D) 子集契约：**补登记**而不是**删主体**（后者会静默丢人）==")
plan = base(participants=["林工"], reference_participants=["林工", "助手", "路人"])
ok, got = run(plan)
check("子集违约能被修好", ok, str(got)[:80] if not ok else "")
if ok:
    check("核心图三个主体都保留（没被删）",
          got["reference_participants"] == ["林工", "助手", "路人"],
          str(got["reference_participants"]))
    check("缺的主体被补进 participants",
          all(name in got["participants"] for name in ["助手", "路人"]),
          str(got["participants"]))

print("\n== D2) owner 未登记：同样是补登记，不是改 owner/删文字 ==")
plan = base(beats=[{"action": "x", "texts": [
    {"text": "账目", "owner": "助手", "container": "对话气泡"}]}] * 3)
ok, got = run(plan)
check("owner 未登记能被修好", ok, str(got)[:80] if not ok else "")
if ok:
    check("文字原文未被改动", got["beats"][0]["texts"][0]["text"] == "账目")
    check("owner 未被改动", got["beats"][0]["texts"][0]["owner"] == "助手")
    check("owner 被补进 participants", "助手" in got["participants"], str(got["participants"]))

print("\n== E) 上限常量与 OCV 源码一致（引用而非重定义）==")
source = (PROJECT_ROOT / "backend" / "app" / "video_motion_plan.py").read_text(
    encoding="utf-8", errors="replace")
checks = [
    ("participants 上限 12", r"len\(participants\) > (\d+)", motion_plan_resilience._MAX_NAMES),
    ("主体名上限 160", r"string\(name, (\d+), '主体名称'\)", motion_plan_resilience._MAX_NAME_LEN),
    ("短文字上限 80", r"'text', (\d+), '短文字'", motion_plan_resilience._MAX_TEXT_LEN),
    ("reference_visual 上限 6000",
     r"string\(value\.get\('reference_visual'\), (\d+),", motion_plan_resilience._MAX_REFERENCE_VISUAL),
]
for label, pattern, ours in checks:
    m = re.search(pattern, source)
    check(f"{label} 与源码一致", bool(m) and int(m.group(1)) == ours,
          f"源码 {m.group(1) if m else '?'} vs 补丁 {ours}")

print("\n== F) '画面标注' 是唯一允许不登记在 participants 里的 owner ==")
plan = base(beats=[{"action": "x", "texts": [
    {"text": "注意", "owner": "画面标注", "container": "说明框"}]}] * 3)
ok, got = run(plan)
check("owner=画面标注 合法通过", ok, str(got)[:80] if not ok else "")
if ok:
    check("没有把「画面标注」误当成主体补进去",
          "画面标注" not in got["participants"], str(got["participants"]))

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
