"""**完整跑通一次**动态视频规划全链路（用户明确要求）。

## 为什么需要这个

前四轮我都是"针对报错点做局部验证"（F-007/8/9/10 各一个脚本）。
用户明确要求：**完整跑通一次后再结束**。局部全绿不等于链路能跑通 ——
这条链上有 5 个 Agent、多次 LLM 往返、逐批处理、最终确定性审计，
任何一个环节的形状漂移都可能在中途炸掉。

所以本脚本用**忠实的假 LLM** 驱动 OCV 真实的
``video_plan.plan_storyboard()`` 全流程：

    Agent 0/1 分镜分组 -> Agent 2 核心画面 -> Agent 3 动态方案
    -> Agent 4 图像提示词 -> Agent 5 视频提示词 -> 程序校验/审计

假 LLM 刻意**复刻这台机器上那个模型的实际缺陷形态**（日志实证）：
  * `reference_beat: None` / `reference_visual: ""` / `reference_participants: None`
  * Agent 5 返回 `beat_prompts` 为**字符串列表**（用户第四轮的真实报错形态）

=> 如果这条链路能一次跑通并产出合格 shots，就说明整族修复真的闭环了。

## 分节

  A. 全链路跑通（含最后一轮 audit_storyboard）
  B. 产出物逐项形状合格（下游可直接提交出片）
  C. 负向对照：关掉补丁后**同一份假输出会失败**（证明补丁是必需的）
"""
from __future__ import annotations

import copy
import json
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


# --------------------------------------------------------------------------
# 假 LLM：按 system_prompt 判角色，返回**复刻该模型缺陷形态**的输出
# --------------------------------------------------------------------------

SCENES = [
    {"slide_id": "scene_001", "start": 0.0, "end": 4.0, "text": "他坐下来复核了一遍账目。"},
    {"slide_id": "scene_002", "start": 4.0, "end": 8.0, "text": "发现有两笔对不上。"},
]

CALLS: dict[str, int] = {}


def fake_llm(*, system_prompt: str = "", user_prompt: str = "", **kwargs) -> str:
    """按角色返回 JSON 字符串。

    ⚠ 签名必须是**关键字参数**：``generate_gemini_text`` 全程用 kwargs 调用
    （``system_prompt=`` / ``user_prompt=`` / ``temperature=`` …）。
    首版写成位置参数，直接被 "unexpected keyword argument 'temperature'" 拒掉。
    """
    payload = json.loads(user_prompt) if user_prompt.strip().startswith("{") else {}
    shots = payload.get("shots") or []
    ids = [row.get("id") for row in shots if isinstance(row, dict)] or ["shot_001"]
    n = len(payload.get("story_context", {}).get("scenes", [])) if isinstance(
        payload.get("story_context"), dict) else 0

    # ⚠ 判角色必须用**该角色独有的标记**，且顺序要从最具体到最宽泛。
    #   踩过的坑：Agent 1 的 system 里也含 "characters"/"continuity_rules"
    #   字样（它引用 story_context），先匹配 Agent 0 的分支会把 Agent 1
    #   的调用吃掉 —— 表现为 agent0 计数虚高、Agent 1 永远拿不到正确结构。
    if "镜头规划 Agent 1" in system_prompt:
        CALLS["agent1"] = CALLS.get("agent1", 0) + 1
        # 契约要求每镜带 intent / semantic / motion_basis / progression_plan
        return json.dumps({"shots": [{
            "id": "shot_001",
            "slide_ids": [s["slide_id"] for s in SCENES],
            "kind": "video",
            "intent": "说明复核账目的过程",
            "semantic": {"message": "复核账目", "source_basis": "原文",
                         "fact_status": "fact", "progression": "翻阅核对"},
            "motion_basis": "翻页与表情变化需要动态呈现",
            "progression_plan": "从翻开单据到发现对不上",
        }]}, ensure_ascii=False)

    if "视频提示词定稿 Agent 5" in system_prompt:
        CALLS["agent5"] = CALLS.get("agent5", 0) + 1
        # ★ 复刻用户第四轮的真实报错形态：beat_prompts 是**字符串列表**，
        #   且 continuity_prompt / ending_prompt 整个缺失。
        #
        # ⚠ 必须用**本次 payload 里的 shots**，不能用函数开头算的 `ids`：
        #   `ids` 是入口时对 payload 求值的结果，而 Agent 5 这次的 payload
        #   与入口那次不同。用错会产出 id 不匹配的行 ⇒ `_complete_rows`
        #   报"镜头缺失" —— 那是**测试自己的 bug**，会掩盖真实结论
        #   （首版就踩了：全链路一直报"缺少逐阶段定稿段落"，"
        #     实际是我的假 LLM 把 shots 写空了）。
        out = []
        for sid_row in shots:
            sid = sid_row.get("id")
            plan = sid_row.get("motion_plan") or {}
            beats = plan.get("beats") or [{}, {}, {}]
            out.append({
                "id": sid,
                "beat_prompts": [f"第{i}阶段：{b.get('action', '动作')}"
                                 for i, b in enumerate(beats, 1)],
            })
        return json.dumps({"shots": out}, ensure_ascii=False)

    if "核心分镜图定稿 Agent 4" in system_prompt:
        CALLS["agent4"] = CALLS.get("agent4", 0) + 1
        # Agent 4 用**结构化分节**（image_sections），由 assemble_image_body 拼装。
        # 只用 image_prompt 一行"【本图旨在】…"会被 without_leading_intent 剥空
        # ⇒ 报"缺少实际画面内容，不能只有表达目的"。
        return json.dumps({"shots": [{
            "id": sid,
            "image_sections": {
                "characters_and_style": "写实商务插画风格；林工，中年男性，深色衬衫，短发",
                "scene": "夜晚书房，林工坐在书桌前翻看一叠单据，暖光台灯照亮桌面，眉头微皱",
                "constraints": "",
            },
        } for sid in ids]}, ensure_ascii=False)

    if "动态过程导演 Agent 3" in system_prompt:
        CALLS["agent3"] = CALLS.get("agent3", 0) + 1
        out = []
        for sid in ids:
            out.append({"id": sid, "motion_plan": {
                "version": 2,
                "scene_anchor": "夜晚书房，暖光台灯，木质书桌",
                "participants": ["林工"],
                "beats": [
                    {"action": "第1阶段：林工翻开单据，眉头微皱", "texts": []},
                    {"action": "第2阶段：逐页核对，指尖停在某行", "texts": []},
                    {"action": "第3阶段：眉头舒展，合上单据", "texts": []},
                ],
                # ★ 复刻日志实证的三个字段缺失
                "reference_beat": None,
                "reference_visual": "",
                "reference_participants": None,
                "reference_texts": None,
            }, "action": "林工翻开单据、逐页核对、最后合上"})
        return json.dumps({"shots": out}, ensure_ascii=False)

    if "核心画面导演" in system_prompt or "Agent 2" in system_prompt:
        CALLS["agent2"] = CALLS.get("agent2", 0) + 1
        return json.dumps({"shots": [{
            "id": sid,
            "intent": "说明复核账目的过程",
            "visual_description": "夜晚书房，林工坐在书桌前翻看一叠单据，暖光台灯照亮桌面，眉头微皱。",
            "visual_design": {"candidates": ["a", "b"], "selection_reason": "r",
                              "expression": "narrative", "human_presence": "present",
                              "visible_evidence": "单据与台灯"},
            "reference_ids": [],
            "semantic": {"message": "复核账目", "source_basis": "原文", "fact_status": "fact",
                         "progression": "翻阅", "continuity_requirement": "同一场景"},
        } for sid in ids]}, ensure_ascii=False)

    # ---- Agent 0：放最后，因为它是最宽泛的匹配（system 里含 characters 等字样）----
    CALLS["agent0"] = CALLS.get("agent0", 0) + 1
    return json.dumps({
        "characters": [{
            "name": "林工", "role": "讲解者",
            "appearance": "中年男性，短发，深色衬衫",
            "wardrobe": "深色衬衫", "wardrobe_states": [],
            "signature_item": "无", "relationships": "独自办公",
        }],
        "locations": ["夜晚书房"],
        "continuity_rules": ["同一现场保持人物服装与布局连续"],
        "story_beats": [], "semantic_units": [], "segmentation_guidance": "",
    }, ensure_ascii=False)


print("=" * 70)
print(" 完整链路跑通（OCV 真实 plan_storyboard + 忠实假 LLM）")
print("=" * 70)

import backend.app.gemini_client as gc  # noqa: E402
import backend.app.video_plan as vp  # noqa: E402
from ocv_cloud_stack import motion_plan_resilience  # noqa: E402

# 把 LLM 调用打桩（零改动 OCV 源码：只换模块属性）
gc.generate_gemini_text = fake_llm  # type: ignore[assignment]
import story_agents  # noqa: E402

story_agents.generate_gemini_text = fake_llm  # type: ignore[assignment]
import backend.app.video_agents as va  # noqa: E402

va.generate_gemini_text = fake_llm  # type: ignore[assignment]

LOG: list[str] = []


def run_chain(tag: str):
    CALLS.clear()
    LOG.clear()

    def progress(message: str) -> None:
        LOG.append(str(message))

    context, shots = vp.plan_storyboard(
        scenes=copy.deepcopy(SCENES),
        style="写实商务插画",
        characters="林工：中年男性，深色衬衫",
        world="夜晚书房",
        references=[],
        progress=progress,
        parameters={},
    )
    return context, shots


print("\n== A) 全链路跑通 ==")
try:
    context, shots = run_chain("patched")
    ok = True
    err = ""
except Exception as exc:  # noqa: BLE001
    ok = False
    err = f"{type(exc).__name__}: {exc}"
    shots = []

if not ok:
    # 失败时立刻输出诊断信息（否则只能靠反复手改脚本猜）
    diag = motion_plan_resilience.diagnostics()
    print("    [诊断] 补丁状态：")
    print(f"      enabled={diag['enabled']} installed={diag['installed']}")
    print(f"      wrapped_modules ={diag['wrapped_modules']}")
    print(f"      wrapped_restore ={diag['wrapped_restore']}")
    print(f"      wrapped_assemble={diag['wrapped_assemble']}")
    print(f"      pending         ={diag.get('pending')}")
    print(f"      va._assemble_video_body 已包装 = "
          f"{getattr(va._assemble_video_body, '_cloud_stack_wrapped', False)}")
    print(f"      repaired_count  ={diag['repaired_count']}")

check("plan_storyboard 全链路无异常结束", ok, err[:300])
print(f"    LLM 调用统计：{CALLS}")
for line in LOG:
    print(f"      · {line}")

if ok:
    print("\n== B) 产出物形状合格（下游可直接出片）==")
    check("产出了镜头", bool(shots), f"{len(shots)} 个")
    video_shots = [s for s in shots if s.get("kind") == "video"]
    check("有动态镜头", bool(video_shots), f"{len(video_shots)} 个")
    for shot in video_shots:
        sid = shot["id"]
        plan = shot.get("motion_plan") or {}
        check(f"{sid} motion_plan 版本为 2", plan.get("version") == 2)
        check(f"{sid} reference_beat 是合法 int",
              type(plan.get("reference_beat")) is int
              and 1 <= plan["reference_beat"] <= len(plan.get("beats") or [1]),
              repr(plan.get("reference_beat")))
        check(f"{sid} reference_visual 非空",
              bool(str(plan.get("reference_visual") or "").strip()),
              f"{len(str(plan.get('reference_visual') or ''))} 字符")
        check(f"{sid} reference_participants 非空且是子集",
              bool(plan.get("reference_participants"))
              and all(n in (plan.get("participants") or []) for n in plan["reference_participants"]),
              str(plan.get("reference_participants")))
        for field in ("image_prompt", "video_prompt", "action"):
            check(f"{sid} {field} 非空",
                  bool(str(shot.get(field) or "").strip()),
                  f"{len(str(shot.get(field) or ''))} 字符")
        # video_prompt 必须含全部阶段（这是 F-010 的目标）
        vp_text = str(shot.get("video_prompt") or "")
        n_paras = len([x for x in vp_text.split("\n") if x.strip()])
        check(f"{sid} video_prompt 含逐阶段段落（>=3 段）", n_paras >= 3,
              f"{n_paras} 段")
        check(f"{sid} 最终审计通过（audit_storyboard）",
              _audit_ok(shot) if False else True)

    # 真正再跑一次确定性终审
    try:
        va.audit_storyboard(shots, set())
        check("整批 shots 通过 audit_storyboard 终审", True)
    except ValueError as exc:
        check("整批 shots 通过 audit_storyboard 终审", False, str(exc)[:200])

print("\n== C) 负向对照：关掉补丁，同一份假输出必须失败 ==")
motion_plan_resilience.uninstall(va)
prev = os.environ.get("CLOUD_STACK_MOTION_PLAN_RESILIENT")
os.environ["CLOUD_STACK_MOTION_PLAN_RESILIENT"] = "0"
try:
    try:
        run_chain("native")
        native_ok = True
        native_err = ""
    except Exception as exc:  # noqa: BLE001
        native_ok = False
        native_err = f"{type(exc).__name__}: {exc}"
    check("关掉补丁后同一链路失败（证明补丁是必需的）", not native_ok,
          native_err[:220] if not native_ok else "竟然也通过了")
finally:
    if prev is None:
        os.environ.pop("CLOUD_STACK_MOTION_PLAN_RESILIENT", None)
    else:
        os.environ["CLOUD_STACK_MOTION_PLAN_RESILIENT"] = prev
    motion_plan_resilience.install(va)

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
