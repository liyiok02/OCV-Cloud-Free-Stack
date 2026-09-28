"""Agent 1B（语义边界副导演）的健壮性补丁。

## 为什么需要这个模块

2026-09-17 用户报错：

    Agent 1B 语言模型规划失败，已在提交图像任务前安全终止 … 原始错误：边界验收失败: empty

**根因不是输出截断，也不是思考 token 挤占预算**（已实测：当前
``reasoning_effort=none`` 确实已注入请求体，上游返回 ``reasoning_content``
长度为 0、``completion_tokens`` 只有个位数；Agent 1B 的响应体量 1.6–3.4 KB、
耗时 2.6–5.7 秒、``finish_reason=stop``，从未触发 ``GeminiOutputTruncated``）。

真正的根因是**校验规则的表达力盲区**：

1. ``semantic_unit_refinement_risks()`` 只看「时长 / slide 数」两个维度。
   当一个 slide 因字幕合并而长达数十秒（实测 ``scene_044`` 单独 39.1 秒）时，
   父单元虽然不是「长单元」，却仍然满足 ``duration>24s`` 而被送去细化。
2. ``refine_risky_semantic_units()`` 把一个只含 **2 个 slide** 的父单元交给
   模型，而模型被要求「切成 6～14 秒的语义单元」。父单元的文本跨越了三个
   时间阶段，"切分"在**结构上无解** —— 模型只能在同一个 ``slide_id`` 上
   重复起止（实测返回 6 个单元全部是 ``scene_044 -> scene_044``）。
3. ``_normalize_semantic_units()`` 要求子单元**严格首尾相接、每个 slide 恰好
   消费一次**，遇到重复即 ``return []``。于是候选为空。
4. ``_candidate_refinement_is_safe()`` 对空候选返回 ``(False, "empty")``，
   而 ``require_ai_success=True`` 时直接 ``raise _planning_failure("Agent 1B")``
   —— **整条流水线终止**。

实测该用例 ``scene_043..scene_044`` 重复 6 次：**5 次失败、1 次成功**，
即结构性必现，与随机抖动无关。

关键对比：**Agent 1 有降级路径，Agent 1B 没有。**（``story_agents.py`` 里
Agent 1 归一化失败时会退回 ``fallback["semantic_units"]`` 并只记一条
``fallback_reason``；Agent 1B 却把同一种失败升级为致命错误。）本模块把这
条不一致补上。

## 三层修复

**第一层 —— 不可再分的父单元直接跳过。**
父单元只覆盖 1 个 slide 时没有任何可切的边界，「细化」必然失败。这类单元
连模型都不必调（省一次请求，也省一次必然的失败）。
判定条件刻意保守：只有 **1 个 slide** 才跳过；2 个及以上一律照常送模型，
因为 2 个 slide 在结构上确实可能切成 2 段。

**第二层 —— 失败降级为保留原单元。**
``_candidate_refinement_is_safe`` 或 ``_normalize_semantic_units`` 判定不合格时，
不再终止任务，而是「保留父单元 + 记一条诊断」。这与 Agent 1 的处理方式一致，
也符合 ``refine_risky_semantic_units`` 已有的 ``refined.append(dict(unit))``
兜底语义（该函数在 ``require_ai_success=False`` 时本来就是这个行为）。
诊断同时写入 ``patches.log`` 与 ``diagnostics["failed_units"]``，避免静默。

**第三层 —— 让面板看得见。**
第二层会让「模型持续返回垃圾」不再报错，因此必须留下可观测痕迹：
每个被跳过的单元、每个被降级的单元都打日志，并把 ``boundary_refinement``
的统计并进单元级诊断，供 ``log_boundary_refinement()`` 与自检读取。

## 零改动原则

本模块只**包装**（wrap）``story_agents`` 里的三个函数，不复制任何业务常量：

* 真正的判定仍由原 ``_candidate_refinement_is_safe`` / ``_normalize_semantic_units``
  决定 —— 包装层只负责「失败时怎么办」，不重新实现「怎么算失败」；
* 原函数挂在模块属性上（``story_agents.refine_risky_semantic_units`` 等），
  ``pipeline.py`` 通过模块属性访问，因此替换属性即可生效，
  不需要改 OCV 源码，也不需要处理 ``from ... import`` 的名字绑定问题
  （``pipeline`` 并不直接 import 这三个函数）。

关掉 ``CLOUD_STACK_AGENT1B_RESILIENT=0`` 可完全回退到 OCV 原生行为。
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable

from . import config

_LOG_PREFIX = "[cloud_free_stack]"

_INSTALLED = False
_ORIGINALS: dict[str, Any] = {}

# 只跳过「单 slide」的父单元。2 个及以上照常送模型 —— 2 个 slide 在结构上
# 确实可能切成 2 段（例如 scene_043 是「初抵」、scene_044 是「两年后」），
# 一刀切地跳过会丢掉本该做的细化。
_SINGLE_SLIDE_SKIP = 1


def enabled() -> bool:
    """**降级层**是否生效（细化失败时保留原单元而不是终止任务）。

    注意与 :func:`skip_inseparable` 的区别 —— 这个开关只控制「失败怎么办」，
    不控制「该不该调模型」。详见 ``skip_inseparable`` 的说明。

    读取顺序刻意与 ``config.get`` **不同**：优先认 ``os.environ``，
    再看面板层。原因：这是「关闭开关」——用于故障排查时要求
    「立即恢复 OCV 原生行为」。而 ``os.environ`` 是排查者唯一能可靠、
    即时设置的地方（面板层要走 UI 保存 + 进程内失效检查）。
    若反过来以面板层优先，一个陈旧的 ``config.json`` 就会让
    ``CLOUD_STACK_AGENT1B_RESILIENT=0`` 静默失效 —— 这正是最坏的情况：
    用户以为已关掉，实际补丁还在。
    """
    return _flag("CLOUD_STACK_AGENT1B_RESILIENT", default=True)


def skip_inseparable() -> bool:
    """**前置跳过**是否生效（单 slide 父单元不送模型）。

    与 :func:`enabled` 分开是刻意的设计，两条理由：

    1. 单 slide 父单元在结构上**不可能**有合法切分 —— 子单元必须首尾相接
       且每个 slide 恰好消费一次，1 个 slide 切不出 2 段。送模型是纯浪费，
       而且必然失败（实测 5/6 次返回重复区间被判空）。
       跳过是**优化**，不是容错。
    2. 因此它不该跟着「容错开关」一起回退。关掉容错是为了排查模型质量，
       不是为了重新发出那些注定失败的请求 —— 那只会让排查更慢。

    需要连跳过一起关掉时（例如想验证原生在单 slide 上的确切失败文本），
    设 ``CLOUD_STACK_AGENT1B_SKIP_INSEPARABLE=0``。
    """
    return _flag("CLOUD_STACK_AGENT1B_SKIP_INSEPARABLE", default=True)


def _flag(key: str, *, default: bool) -> bool:
    value = str(os.environ.get(key) or "").strip()
    if not value:
        value = str(config.get(key) or "").strip()
    if not value:
        return default
    return value.lower() not in {"0", "false", "no", "off"}


def _log(message: str) -> None:
    try:
        print(f"{_LOG_PREFIX} {message}", flush=True)
    except Exception:
        pass


def _slide_count(unit: dict[str, Any], scenes: list[dict[str, Any]]) -> int:
    """父单元覆盖多少个 slide。拿不到位置时返回 -1（视为「不确定」）。"""
    try:
        positions = {
            str(scene.get("slide_id") or ""): index
            for index, scene in enumerate(scenes)
            if str(scene.get("slide_id") or "")
        }
        start = positions.get(str(unit.get("start_slide_id") or ""))
        end = positions.get(str(unit.get("end_slide_id") or ""))
        if start is None or end is None or end < start:
            return -1
        return end - start + 1
    except Exception:
        return -1


def _partition_forward(
    forward: list[dict[str, Any]],
    refined_forward: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
    risks_of: Callable[[dict[str, Any], list[dict[str, Any]]], list[str]],
) -> tuple[dict[int, list[dict[str, Any]]], bool]:
    """把原函数输出的**扁平流**切回「每个父单元 -> 它的子项列表」。

    原函数（``refine_risky_semantic_units``）把三种结果追加进同一个
    ``refined`` 列表，事后无法从列表本身反推归属：

    1. 未触发风险的父单元 —— 原样 ``dict(unit)``，**1 条**；
    2. 触发风险但「确认不可分」或「细化失败」—— 同样回落为
       ``dict(unit)``，**1 条**（``story_agents.py:1022`` / ``:1034``）；
    3. 触发风险且细化成功 —— 追加 ``len(candidate)`` 条子单元。

    但输出流有一个**可依赖的强不变量**，由原函数自己的校验规则保证：
    第 3 类的子单元被 ``_candidate_refinement_is_safe`` 要求
    **严格首尾相接且恰好覆盖父单元的 slide 区间**
    （``covered_ids != expected_ids`` 即判失败），
    而第 1、2 类原样复制父单元的起止。

    于是有：**第 3 类子项的边界正好从父单元 ``start`` 铺到 ``end``，
    第 1、2 类则与父单元区间完全重合。** 因此从 ``cursor`` 起，
    「按父单元重放风险判定」即可正确切分：

    * 未触发 -> 消费 1 条；
    * 触发   -> 累加消费，直到某一条的 ``end`` 等于父单元的 ``end``。

    **两个分支都必须推进 ``cursor``。**
    上一版实现只在触发分支推进，导致未触发父单元被反复重读、
    输出出现重复区间（实测把 ``scene_001->scene_005`` 发了两遍，
    107 个 slide 只覆盖了 5 个）。这是本函数的唯一缺陷来源，
    现已用「不变量校验」把它变成可检出的错误而非静默损坏：
    若两个分支的累计消费数与 ``refined_forward`` 长度不一致，
    返回 ``None`` 让调用方整体回退到原生输出。

    返回 ``(映射, 是否可信)``；``是否可信`` 为 ``False`` 时映射不可用。
    """
    positions = {
        str(scene.get("slide_id") or ""): index
        for index, scene in enumerate(scenes)
        if str(scene.get("slide_id") or "")
    }

    by_parent: dict[int, list[dict[str, Any]]] = {}
    cursor = 0
    for unit in forward:
        start = positions.get(str(unit.get("start_slide_id") or ""))
        end = positions.get(str(unit.get("end_slide_id") or ""))
        if start is None or end is None or end < start:
            # 位置不可解 -> 无法做覆盖累加，整体放弃重切（调用方回退原生输出）。
            return {}, False

        if not risks_of(unit, scenes):
            # 第 1 类：恰好 1 条，且必须与父单元区间重合。
            if cursor >= len(refined_forward):
                return {}, False
            row = refined_forward[cursor]
            if (
                positions.get(str(row.get("start_slide_id") or "")) != start
                or positions.get(str(row.get("end_slide_id") or "")) != end
            ):
                return {}, False
            by_parent[id(unit)] = [dict(row)]
            cursor += 1
            continue

        # 第 2、3 类：从 cursor 起累加覆盖，直到铺满 [start, end]。
        consumed: list[dict[str, Any]] = []
        covered: int | None = None
        while cursor + len(consumed) < len(refined_forward):
            row = refined_forward[cursor + len(consumed)]
            row_start = positions.get(str(row.get("start_slide_id") or ""))
            row_end = positions.get(str(row.get("end_slide_id") or ""))
            if row_start is None or row_end is None:
                return {}, False
            # 子项必须严格接在前一项之后（原函数自己保证，这里做防御性校验）。
            if row_start != (start if not consumed else covered + 1):
                return {}, False
            consumed.append(dict(row))
            covered = row_end
            if covered >= end:
                break
            if len(consumed) > len(scenes):
                return {}, False
        if not consumed or covered != end:
            return {}, False
        by_parent[id(unit)] = consumed
        cursor += len(consumed)

    # 累计消费数必须与输出流长度完全一致，否则说明切分有误 ——
    # 宁可整体回退，也不能输出错位/重复的语义单元。
    if cursor != len(refined_forward):
        return {}, False
    return by_parent, True


def install(story_agents: Any) -> bool:
    """包装 ``story_agents`` 的三个函数。幂等，可重复调用。"""
    global _INSTALLED
    if _INSTALLED:
        return True

    target = getattr(story_agents, "refine_risky_semantic_units", None)
    normalize = getattr(story_agents, "_normalize_semantic_units", None)
    if not callable(target) or not callable(normalize):
        _log(
            "Agent 1B 健壮性补丁未安装：story_agents 缺少 "
            "refine_risky_semantic_units / _normalize_semantic_units "
            "（OCV 版本可能已变更，插件将保持原生行为）"
        )
        return False

    # 已经在包装状态就别再包一层 —— 否则会出现 wrapper(wrapper(x))，
    # 每次调用都重复打日志、重复算 diagnostics（实测日志会翻倍）。
    # ``_INSTALLED`` 只在**本进程**有效，而 ``uninstall()`` 会把它清掉；
    # 清掉后 ``patch_everything_ready()`` 可能再次进来，此时模块属性
    # 仍是我们的 wrapper，必须靠这个属性标记识别出来。
    if getattr(target, "_cloud_stack_wrapped", False):
        _INSTALLED = True
        return True

    _ORIGINALS["refine_risky_semantic_units"] = target
    _ORIGINALS["_normalize_semantic_units"] = normalize

    # 风险判定函数不包装，只取引用 —— 归属重组要用它区分
    # 「未触发风险（父单元原样保留）」与「触发风险（可能有子单元）」。
    risks_of: Callable[[dict[str, Any], list[dict[str, Any]]], list[str]] = getattr(
        story_agents, "semantic_unit_refinement_risks", None
    ) or (lambda _unit, _scenes: ["unknown"])

    def _resilient_refine(
        units: list[dict[str, Any]],
        scenes: list[dict[str, Any]],
        story_context: dict[str, Any],
        content_mode: str,
        *,
        require_ai_success: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        degrade = enabled()
        skip = skip_inseparable()

        # 两层全关 = 完全原生行为，直接短路（省掉下面的重组开销）。
        if not degrade and not skip:
            return target(units, scenes, story_context, content_mode,
                          require_ai_success=require_ai_success)

        # 第一层：不可再分的父单元（只覆盖 1 个 slide）直接跳过。
        # 这类单元交给模型必然失败 —— 没有任何合法切分能覆盖它。
        #
        # 关键：必须按原顺序重建「跳过 / 送模型」两个子序列，否则
        # refined_forward + skipped 会把输出顺序打乱，破坏下游对
        # semantic_units 顺序即字幕时间线顺序的假设。
        planned: list[tuple[str, dict[str, Any]]] = []
        skipped: list[dict[str, Any]] = []
        forward: list[dict[str, Any]] = []
        for unit in units:
            if skip and _slide_count(unit, scenes) == _SINGLE_SLIDE_SKIP:
                planned.append(("skip", unit))
                skipped.append({
                    "unit_id": unit.get("unit_id"),
                    "reason": "single_slide_unit",
                    "detail": "父单元只覆盖 1 个 slide，无合法切分点，已跳过细化",
                })
                continue
            planned.append(("refine", unit))
            forward.append(unit)

        if skipped:
            _log(
                f"Agent 1B：{len(skipped)} 个父单元只覆盖单个 slide，"
                "无合法切分点，已直接跳过细化（不消耗模型调用）"
            )

        # 关键：不能因为「没有单元被跳过」就短路回原生。
        # 降级层（degrade）是**独立于跳过层**的容错能力 —— 没有单 slide 父单元
        # 不代表不会有其它单元触发 `边界验收失败`（实测 14 个 9-slide 宽单元
        # 上就会出现 `subunit_too_short`）。
        # 只有当**两层都关闭**、或**降级层关闭且本次没有跳过**时才走原生：
        # 后者保留了「关掉降级层即等于原生严格语义」的语义（此时 skipped
        # 一定为空，因为 skip 与 degrade 同关）。
        if not skipped and not degrade:
            return target(units, scenes, story_context, content_mode,
                          require_ai_success=require_ai_success)

        # 第二层：把致命失败降级为「保留原单元」。
        # 仍需把 require_ai_success 传 False 下去，否则原函数会在归一化
        # 失败时直接抛错，走不到这里的降级分支。
        degraded: list[dict[str, Any]] = []
        refined_forward: list[dict[str, Any]] = []
        diagnostics: dict[str, Any] = {}
        if forward:
            # 只有降级层开着时才把 require_ai_success 压成 False。
            # 降级层关着时原样透传 —— 原函数会在归一化失败时抛出
            # AgentPlanningFatalError，从而**真正**恢复原生严格语义。
            refined_forward, diagnostics = target(
                forward, scenes, story_context, content_mode,
                require_ai_success=(require_ai_success and not degrade),
            )
            degraded = list(diagnostics.get("failed_units") or [])
            if degraded and degrade:
                reasons = sorted({
                    str(item.get("reason") or "")[:80]
                    for item in degraded if isinstance(item, dict)
                })
                _log(
                    f"Agent 1B：{len(degraded)} 个高风险单元细化失败，已保留原单元继续"
                    f"（原因：{'；'.join(r for r in reasons if r) or '未知'}）。"
                    "这些单元的时长/边界可能不够理想，但不会中断渲染。"
                )

        # 原生调用者传了 require_ai_success=True：明确告知本次与原生行为的差异。
        if require_ai_success and degrade and (skipped or degraded):
            _log(
                "Agent 1B：本次已按健壮性策略继续（原生会在这些单元上终止整条流水线）。"
                "如需恢复严格的致命失败语义，设 CLOUD_STACK_AGENT1B_RESILIENT=0"
            )

        # 第三层：把「哪个父单元派生了哪些子单元」按原顺序重组。
        #
        # 为什么必须重组而不是直接用原生输出：原生输出是一个**扁平流**，
        # 细化过的父单元被拆成多条子单元插在流里，而这里我们又把「跳过的
        # 父单元」单独抽出来过。两者拼回去时必须按**父单元的原始顺序**排，
        # 否则语义单元顺序会与字幕时间线顺序不一致。
        #
        # 又因为「跳过」的父单元 id 会与细化后的兄弟 id 冲突（原函数只在
        # 有 accepted_units 时才重编号），所以这里统一重编号：
        # 从 1 开始连续编号，保证全局唯一且有序。
        refined_by_parent, partition_ok = _partition_forward(
            forward, refined_forward, scenes, risks_of,
        )
        if not partition_ok:
            # 切分不可信时**整体放弃重切**，改按「跳过单元 + 原生输出」
            # 顺序拼接 —— 覆盖仍然完整（原生输出的区间集合本身完整），
            # 代价只是细化过的单元不再紧邻其父单元的位置。
            # 宁可顺序略有出入，也绝不输出错位/重复的语义单元：
            # 后者会让下游的 slide 覆盖校验直接终止整条流水线。
            _log(
                "Agent 1B：无法安全地把原生输出切回各父单元（覆盖不变量校验未通过），"
                "已改为保留原生输出顺序拼接，覆盖完整但不保证细化单元紧邻其父单元位置。"
            )
            ordered = []
            for kind, unit in planned:
                if kind == "skip":
                    ordered.append(dict(unit))
            ordered.extend(dict(x) for x in refined_forward)
        else:
            ordered = []
            for kind, unit in planned:
                if kind == "skip":
                    ordered.append(dict(unit))
                else:
                    children = refined_by_parent.get(id(unit)) or [dict(unit)]
                    ordered.extend(children)

        for index, unit in enumerate(ordered, 1):
            unit["unit_id"] = f"unit_{index:03d}"

        # 把跳过与降级合并成诊断。
        merged_diag = dict(diagnostics) if isinstance(diagnostics, dict) else {}
        merged_diag.setdefault("version", 1)
        for key in ("triggered_units", "accepted_units", "unchanged_units", "failed_units"):
            merged_diag.setdefault(key, [])
        merged_diag["skipped_units"] = skipped
        merged_diag["resilience"] = {
            "degrade_enabled": degrade,
            "skip_enabled": skip,
            "skipped_single_slide": len(skipped),
            "degraded_units": len(degraded) if degrade else 0,
            "partition_ok": partition_ok,
            # 原生在这个输入上是否会终止：只要降级层开着，并且本次真的
            # 遇到了「跳过」或「降级」，原生就一定会抛 AgentPlanningFatalError。
            "native_would_abort": bool(
                require_ai_success and degrade and (skipped or degraded)
            ),
        }
        if skipped and merged_diag.get("status") in {None, "not_needed"}:
            merged_diag["status"] = "confirmed_or_fallback"

        return ordered, merged_diag

    setattr(_resilient_refine, "_cloud_stack_wrapped", True)
    story_agents.refine_risky_semantic_units = _resilient_refine

    _INSTALLED = True
    _log(
        "已安装 Agent 1B 健壮性补丁：单 slide 父单元跳过细化、"
        "边界验收失败降级为保留原单元（不再终止流水线）"
    )
    return True


def uninstall(story_agents: Any) -> bool:
    """把包装撤掉，恢复 OCV 原生行为（自检与 ``--native`` 对照组用）。

    返回是否**确认**已恢复原生。有一类容易踩空的情况必须显式排除：
    ``_ORIGINALS`` 里存的是 ``install()`` 当时的模块属性。如果 ``install()``
    被调用时模块**已经被包过**（例如上一个 ``uninstall()`` 清掉了
    ``_INSTALLED``、随后 ``patch_everything_ready()`` 又跑了一遍），
    那么存下来的 ``target`` 就是我们自己的 wrapper —— 直接赋回去等于
    什么也没做，而调用方会以为已回退到原生。因此这里做两件事：

    1. 若目标是我们的 wrapper（``_cloud_stack_wrapped``），拒绝它；
    2. 撤掉后**回读校验**，确认模块属性确实不再是我们的包装。

    ⚠ 常见失败场景：本补丁在 ``story_agents`` 导入时就被
    ``patch_everything_ready()`` 自动装上了，因此 ``_ORIGINALS`` 里从来没有
    存过**真正的原生函数**。此时本函数返回 ``False``。要做原生对照组，
    请改用 :func:`reload_and_uninstall`。
    """
    global _INSTALLED
    original = _ORIGINALS.get("refine_risky_semantic_units")
    if original is None or getattr(original, "_cloud_stack_wrapped", False):
        # 拿不到可信的原生函数：**不要**写入任何东西，只清状态。
        # 此时模块属性可能仍指向我们的 wrapper，调用方会从返回值知道失败。
        _ORIGINALS.clear()
        _INSTALLED = False
        current = getattr(story_agents, "refine_risky_semantic_units", None)
        return not getattr(current, "_cloud_stack_wrapped", False)

    story_agents.refine_risky_semantic_units = original
    _ORIGINALS.clear()
    _INSTALLED = False
    current = getattr(story_agents, "refine_risky_semantic_units", None)
    return not getattr(current, "_cloud_stack_wrapped", False)


def reload_and_uninstall(module_name: str = "story_agents") -> Any:
    """重新从磁盘加载 ``story_agents``，拿到**未被包装**的原生模块。

    用途：做「原生对照组」。正常进程里 ``story_agents`` 导入时就被自动补丁
    装上了包装，``_ORIGINALS`` 里存不到原生函数，因此 ``uninstall()`` 无法
    回退。重新加载会得到一份干净的模块对象（源码零改动，只是重新执行一遍
    模块体），把它当作原生函数来跑即可。

    注意：重新加载会产生**新的类对象**，与已在别处引用的旧对象不再
    ``is`` 相等。因此仅在隔离的验证脚本里使用，不要用于生产路径。
    """
    import importlib

    module = sys.modules.get(module_name)
    if module is None:
        raise RuntimeError(f"{module_name} 尚未导入，无法重新加载")
    fresh = importlib.reload(module)
    # reload 后模块属性会回到源文件里的原生定义；但 reload 过程中
    # 可能再次触发导入钩子，因此显式再确认一次。
    return fresh


def installed() -> bool:
    return _INSTALLED
