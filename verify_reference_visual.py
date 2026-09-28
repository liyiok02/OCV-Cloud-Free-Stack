"""复现并验证 F-008：`参考画面状态为空、格式无效或过长` 的修复路径不可达。

用户报错（第二轮）：
    动态阶段方案修订仍未通过：动态阶段方案的参考画面状态为空、格式无效或过长

## 结构性根因（本脚本首先复现它）

``restore_reference_draft(plan, visual_description)`` 本意就是用 Agent 2 的
``visual_description`` 草案修复 ``reference_visual``，但实现顺序是**先 normalize
再修复**，于是「空 / 超长」这两种情况在第 157 行就抛了，第 160 行的修复
**永远执行不到** ⇒ 修复路径不可达。

## 本脚本验四件事

  A. 负向对照：关掉补丁后，空/超长**仍然抛错**（复现故障）
  B. 正向：开着补丁，走**真实 `restore_reference_draft`**，两种坏值都被草案修好
  C. 不误伤：合法的 `reference_visual` 零改动；草案也缺失时不编造（交原生报错）
  D. `_MAX_REFERENCE_VISUAL` 与源码里的业务上限一致（引用而非重定义）

用真实的 ``restore_reference_draft`` / ``normalize_motion_plan``，不自己模拟。
"""
from __future__ import annotations

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


ERR = "动态阶段方案的参考画面状态为空、格式无效或过长"
DRAFT = (
    "夜晚书房，暖光台灯照亮木质书桌。林工坐在桌前，双手摊开一叠单据逐页翻看，"
    "眉头先紧后松；桌面右侧一只白色马克杯冒着热气，背景书架虚化。"
)


def plan_with(reference_visual, beats=3):
    return {
        "version": 2,
        "scene_anchor": "夜晚书房，暖光台灯，木质书桌",
        "participants": ["林工"],
        "beats": [{"action": f"第{i}阶段：林工翻动资料", "texts": []} for i in range(1, beats + 1)],
        "reference_beat": 1,
        "reference_visual": reference_visual,
        "reference_participants": ["林工"],
        "reference_texts": [],
    }


def run_restore(reference_visual, draft=DRAFT):
    """走真实的 restore_reference_draft，返回 (成功?, 值或错误)。"""
    try:
        out = vmp.restore_reference_draft(plan_with(reference_visual), draft)
        return True, out.get("reference_visual")
    except ValueError as exc:
        return False, str(exc)


print("== A) 负向对照：关掉补丁，空/超长必须复现用户报错 ==")
motion_plan_resilience.uninstall(vmp)
prev = os.environ.get("CLOUD_STACK_MOTION_PLAN_RESILIENT")
os.environ["CLOUD_STACK_MOTION_PLAN_RESILIENT"] = "0"
try:
    for raw, desc in (("", "空字符串"), ("   ", "纯空白"), (None, "null"),
                      (123, "整数"), ("x" * 6001, "超长 6001")):
        ok, got = run_restore(raw)
        check(f"原生：{desc} 抛「参考画面状态为空、格式无效或过长」",
              (not ok) and got == ERR, got if not ok else f"竟然通过 -> {str(got)[:40]}")
    # 标签路径本来就是为它写的 —— 这条在原生下也**能**修
    ok, got = run_restore("图1")
    check("原生：标签「图1」可被草案替换（说明修复代码本身存在）",
          ok and got == DRAFT.strip(), str(got)[:50])
finally:
    if prev is None:
        os.environ.pop("CLOUD_STACK_MOTION_PLAN_RESILIENT", None)
    else:
        os.environ["CLOUD_STACK_MOTION_PLAN_RESILIENT"] = prev

print("\n== B) 正向：开着补丁，同样的坏值必须被草案修好 ==")
motion_plan_resilience.install(vmp)
for raw, desc in (("", "空字符串"), ("   ", "纯空白"), (None, "null"),
                  (123, "整数（非 str）"), ("x" * 6001, "超长 6001")):
    ok, got = run_restore(raw)
    if not ok:
        check(f"补丁：{desc} 被修好", False, f"仍然抛错：{got}")
        continue
    check(f"补丁：{desc} 被修好且等于草案", got == DRAFT.strip(),
          f"{len(str(got))} 字符")

print("\n== B2) 超长但内容有效、且**没有草案**时可截断（而非报错）==")
out, note = motion_plan_resilience.repair_reference_visual(plan_with("y" * 6001))
check("超长被截断到上限", len(out["reference_visual"]) == motion_plan_resilience._MAX_REFERENCE_VISUAL,
      f"{len(out['reference_visual'])} 字符")
check("截断有说明（不静默）", "截断" in note, note)

print("\n== C) 不误伤：合法值零干预 ==")
legal = "林工坐在书桌前翻看资料"
out, note = motion_plan_resilience.repair_reference_visual(plan_with(legal))
check("合法 reference_visual 原样（原对象返回、无说明）", out is not None and note == "")
ok, got = run_restore(legal)
check("走 restore 后合法值也不变", ok and got == legal, str(got)[:40])

# 边界：正好等于上限不算超长
edge = "z" * motion_plan_resilience._MAX_REFERENCE_VISUAL
out, note = motion_plan_resilience.repair_reference_visual(plan_with(edge))
check("正好 6000 字符不算超长（边界不误伤）", note == "")

print("\n== C2) 草案也缺失时**不编造**，交原生报错（不静默产出错画面）==")
for empty_draft in ("", "   ", None):
    ok, got = run_restore("", draft=empty_draft)
    check(f"草案为 {empty_draft!r} 时不编造、报原生错",
          (not ok) and got == ERR, got if not ok else f"竟然通过 -> {str(got)[:40]}")

# 但超长 + 无草案：可以截断（内容本身有效）
ok, got = run_restore("y" * 6001, draft="")
check("超长 + 无草案：按上限截断通过（内容有效，只是长）",
      ok and len(str(got)) == motion_plan_resilience._MAX_REFERENCE_VISUAL,
      f"{len(str(got)) if ok else got}")

print("\n== D) 上限必须与 OCV 源码一致（引用而非重定义）==")
source = (PROJECT_ROOT / "backend" / "app" / "video_motion_plan.py").read_text(
    encoding="utf-8", errors="replace")
match = re.search(r"string\(value\.get\('reference_visual'\),\s*(\d+),\s*'参考画面状态'\)", source)
check("能从源码抠出 reference_visual 的上限", bool(match),
      match.group(0) if match else "正则没匹配到")
if match:
    check("补丁的上限与源码一致（源码改了就必须同步）",
          int(match.group(1)) == motion_plan_resilience._MAX_REFERENCE_VISUAL,
          f"源码 {match.group(1)} vs 补丁 {motion_plan_resilience._MAX_REFERENCE_VISUAL}")

print("\n== E) 两个函数都装上了（F-007 + F-008 缺一不可）==")
diag = motion_plan_resilience.diagnostics()
check("normalize_motion_plan 已包装", bool(diag["wrapped_modules"]), str(diag["wrapped_modules"]))
check("restore_reference_draft 也已包装（否则修复路径仍不可达）",
      bool(diag.get("wrapped_restore")), str(diag.get("wrapped_restore")))

print("\n== F) 开关：置 0 后回到原生严格行为 ==")
os.environ["CLOUD_STACK_MOTION_PLAN_RESILIENT"] = "0"
try:
    check("置 0 后 enabled()=False", motion_plan_resilience.enabled() is False)
    ok, got = run_restore("")
    check("置 0 后空值重新报原生错", (not ok) and got == ERR,
          got if not ok else f"竟然通过 -> {str(got)[:40]}")
finally:
    os.environ.pop("CLOUD_STACK_MOTION_PLAN_RESILIENT", None)
check("恢复后重新生效", motion_plan_resilience.enabled() is True)

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
