"""动态视频阶段方案（motion_plan）的健壮性补丁 —— **整族记账/格式字段**。

本模块修**四个**已被实测复现的致命缺陷，它们本质相同：
**校验规则的表达力盲区被升级成了终止整条动态视频生产的致命错误。**

## 为什么是"整族"而不是逐个字段

用户连续三轮撞到**同一个 LLM 输出的不同字段**：

    ① reference_beat          -> 核心参考图对应的阶段编号无效       (F-007)
    ② reference_visual        -> 参考画面状态为空、格式无效或过长     (F-008)
    ③ reference_participants  -> 核心参考图主体列表无效              (F-009)

逐个打补丁 = 让用户逐个撞下一个（打地鼠）。所以本模块改为按**整族**处理：
先把 ``normalize_motion_plan`` 的全部 13 个 raise 点枚举出来，再按
**「只修记账/格式，绝不修语义」**分类，一次修完可修的那一类。

### ① ``reference_beat``（记账序号）

一个指向 ``beats`` 的指针，不是语义内容。模型常见偏差：越界、类型漂移
（``"1"`` / ``1.0`` / ``null`` / 缺失）。
**不修还会 ``IndexError``**（它被当数组下标用）。

### ② ``reference_visual``（参考画面状态）+ **修复路径不可达**

原生 ``restore_reference_draft(plan, visual_description)`` 本就是为修它而写的，
但实现顺序是**先 normalize 再修复** ⇒ 空/超长时在第 157 行就抛了，
第 160 行的修复**永远执行不到**（实测三分对照确认）。
修法：**把"填草案"提到 normalize 之前** —— 额外包住 ``restore_reference_draft``。

### ③ ``reference_participants`` / ``participants``（主体名列表）

原生只接受「长度 ≤12 的字符串列表」。模型会写成 ``null``、标量字符串、
含占位符（``null``/``{}``/数字）的列表，或让核心图主体不是 participants
的子集。这些都是**纯形状/记账漂移，意图可还原**。

**关于两套"登记表"（实测踩到的坑）**：
``beats[].texts`` 的 owner 对 ``participants``，
而 ``reference_texts`` 的 owner 对 **``reference_participants``** ——
只补前者，后者的归属仍然非法。

### ④ ``beats[].texts`` / ``reference_texts``（可见短文字）

条目非对象、三要素缺失、重复、超限、owner 未登记 —— 同样按
「丢弃占位符 / 去重 / 截断 / 补登记」处理。

## 分类原则（**这条比任何实现细节都重要**）

| 类别 | 例子 | 处理 |
|---|---|---|
| **可修**（意图可还原） | 形状非 list、含占位符、重名、超限、类型漂移、owner 未登记、超长 | 本模块修 |
| **严格**（编造会静默产出错数据） | ``version`` 非法、阶段数 0 或 >6、beat 非对象、文案/画面**为空且无草案** | **照常报错** |

两条边界值得单独说明：

* ``participants=[""]`` 是**占位符**，而契约明写「无固定主体**可以为空**」
  ⇒ 规整成 ``[]`` 是**正确的形状修复**（``[]`` 是合法值），不是放宽语义。
* ``reference_visual`` 为空**且草案也缺失**时**不编造** ——
  此时确实没有任何可信画面依据，凭空编一段会静默产出与用户原文不符的画面，
  比报错更糟。

## 包装而非复制（以及两个必须小心的陷阱）

本模块只**包装**（wrap）原函数，不复制任何业务校验常量：真正"怎么算合格"
仍由 OCV 原生实现决定，本层只负责"把字段修成合格"。
``_MAX_*`` 常量是**引用**同一批业务上限，自检有断言守着它们与源码一致。

1. **``from ... import`` 绑定陷阱**：``video_agents`` / ``video_plan`` /
   ``video_prompt_refresh`` 都把函数对象直接绑进了自己的命名空间
   ⇒ 只替换定义处**没用**，必须连调用方模块属性一起换（铁律 29）。
2. **修复路径可达性**：要修的字段若在校验那步就抛错，排在后面的修复代码
   等于不存在 ⇒ 必须把修复**提到** normalize **之前**（铁律 31）。

## 开关

``CLOUD_STACK_MOTION_PLAN_RESILIENT``（默认 **1**）。置 0 完全回退原生行为。
读取顺序与 Agent 1B 一致：**先看 ``os.environ`` 再看面板层**
（这是"关闭开关"，陈旧的面板值让它静默失效是最坏情况）。
"""

from __future__ import annotations

import copy
import os
import re
import sys
import threading as _threading
from typing import Any

from . import config

_LOG_PREFIX = "[cloud_free_stack]"

_INSTALLED = False
_ORIGINAL: Any = None
_ORIGINAL_RESTORE: Any = None
_ORIGINAL_ASSEMBLE: Any = None
_WRAPPER: Any = None
_WRAPPER_RESTORE: Any = None
_WRAPPER_ASSEMBLE: Any = None
_REPAIRED: list[dict[str, Any]] = []

# 「属性当时还不存在」的目标模块 -> 已重试次数。
# 见 install_on 的 docstring：``_assemble_video_body`` 在 video_agents
# 模块体靠后位置定义，补丁首次必然拿不到，必须靠后续 import 重试。
_PENDING: dict[str, int] = {}
_PENDING_LOCK = _threading.Lock()
_RETRY_LIMIT = 200
# `reference_visual` 的原生上限（video_motion_plan.string(..., 6000, '参考画面状态')）。
# 这里是**引用**同一个业务上限，不是重新定义规则 —— 自检有断言守着它与源码一致。
_MAX_REFERENCE_VISUAL = 6000

# 其余上限同样**引用** OCV 源码（自检用正则从源码抠出并逐一断言相等）。
_MAX_NAMES = 12        # participants / reference_participants 的条目上限
_MAX_NAME_LEN = 160    # 单个主体名上限
_MAX_TEXT_LEN = 80     # 短文字 / 容器上限
_OWNER_LABEL = "画面标注"   # 唯一允许不登记在 participants 里的 owner

# 所有 `from .video_motion_plan import normalize_motion_plan` 的调用方都要换。
# （`video_motion_plan` 自己也要换，它的内部调用走模块全局查找。）
_TARGET_MODULES: tuple[str, ...] = (
    "backend.app.video_motion_plan",
    "backend.app.video_agents",
    "backend.app.video_plan",
    "backend.app.video_prompt_refresh",
)


def enabled() -> bool:
    """补丁是否生效。默认开启。

    刻意**先读 os.environ 再读面板层**：这是"关闭开关"，排查时要求立即恢复
    原生行为；而环境变量是排查者唯一能即时、可靠设置的地方。若面板层优先，
    一个陈旧的 ``config.json`` 就会让 ``=0`` 静默失效（用户以为关掉了，实际还在）。
    """
    value = str(os.environ.get("CLOUD_STACK_MOTION_PLAN_RESILIENT") or "").strip()
    if not value:
        value = str(config.get("CLOUD_STACK_MOTION_PLAN_RESILIENT") or "").strip()
    if not value:
        return True
    return value.lower() not in {"0", "false", "no", "off"}


def _log(message: str) -> None:
    try:
        print(f"{_LOG_PREFIX} {message}", flush=True)
    except Exception:
        pass


def repair_reference_beat(value: Any) -> tuple[Any, str]:
    """只修 ``reference_beat``，返回 ``(修好的值, 说明)``；无需修改时说明为空串。

    **不做任何超出"记账字段规整"的推断**：不改阶段内容、不改主体、不改文字。
    阶段列表本身有问题（不是非空 list）时直接放行，交给原生校验报它自己的错
    —— 那类错误应当照常暴露，不该被这里掩盖。
    """
    if not isinstance(value, dict):
        return value, ""
    raw_beats = value.get("beats")
    if not isinstance(raw_beats, list) or not raw_beats:
        return value, ""
    total = len(raw_beats)

    current = value.get("reference_beat")
    # 已经合法：零干预（必须是**真正的 int**，不是 bool/float/字符串）
    if type(current) is int and 1 <= current <= total:
        return value, ""

    # 尝试取出"模型想表达的那个数"
    parsed: int | None = None
    if type(current) is int:
        parsed = current
    elif isinstance(current, float) and current.is_integer():
        parsed = int(current)
    elif isinstance(current, str):
        text = current.strip()
        candidate = text[1:] if text[:1] in {"+", "-"} else text
        if candidate.isdigit():
            try:
                parsed = int(text)
            except ValueError:
                parsed = None

    if parsed is None:
        fixed = 1
        note = f"reference_beat 缺失或不可解析（{current!r}），按契约默认取 1"
    else:
        fixed = max(1, min(total, parsed))
        if parsed == fixed:
            # ⚠ 走到这里说明**数值合法但类型不对**（"2" / 2.0）。
            #   不能因为"数值没变"就提前返回 —— 原生的校验是
            #   `type(reference_beat) is not int`，类型不对照样抛错。
            #   实测踩过：首版在此 return，导致字符串 "2" 与浮点 2.0
            #   仍然报「核心参考图对应的阶段编号无效」。
            note = f"reference_beat 类型不是整数（{current!r}），已规整为 {fixed}"
        else:
            note = f"reference_beat 越界（{parsed} 不在 1..{total}），已夹紧为 {fixed}"

    repaired = copy.deepcopy(value)
    repaired["reference_beat"] = fixed
    return repaired, note


def repair_reference_visual(value: Any) -> tuple[Any, str]:
    """规整 ``reference_visual`` 的**格式**问题（无草案可用时）。

    只处理"超长"：把超过 6000 字符的文本按上限截断。
    理由：这是个**长度格式**问题，前缀仍是模型产出的有效画面描述；
    而原生的处理是抛 ``参考画面状态为空、格式无效或过长`` ⇒ 终止整条生产。
    截断丢一点尾部描述，远好于整个动态视频做不出来。

    **空 / 非字符串不在这里修** —— 那种情况需要"权威草案"来填，
    只有 :func:`repair_reference_visual_with_draft` 拿得到草案。
    凭空编一段画面描述比报错更糟（会静默产出与用户原文不符的画面）。
    """
    if not isinstance(value, dict):
        return value, ""
    current = value.get("reference_visual")
    # 非字符串 / 空：交给原生报它自己的错（这里没有可信来源可以填）。
    if not isinstance(current, str) or not current.strip():
        return value, ""
    if len(current) <= _MAX_REFERENCE_VISUAL:
        return value, ""
    repaired = copy.deepcopy(value)
    repaired["reference_visual"] = current[:_MAX_REFERENCE_VISUAL].rstrip()
    return repaired, (
        f"reference_visual 过长（{len(current)} > {_MAX_REFERENCE_VISUAL} 字符），"
        f"已按上限截断"
    )


def repair_reference_visual_with_draft(value: Any, draft: Any) -> tuple[Any, str]:
    """用**权威草案**补/替换不可用的 ``reference_visual``（在 restore 层调用）。

    这是本补丁第二块的核心，修的是 F-008 的**结构性缺陷**：

    OCV 自己的 ``restore_reference_draft(plan, visual_description)`` 本意就是
    「用 Agent 2 的 ``visual_description`` 草案恢复 ``reference_visual``」，
    但它的实现顺序是**先 normalize 再修复**：

        plan = normalize_motion_plan(...)          # ← 空/超长时在这里就抛了
        if not re.fullmatch(标签正则, ...): return plan
        plan['reference_visual'] = visual_description.strip()   # ← 永远执行不到

    ⇒ 对「空 / 超长」这两种情况，**修复路径不可达**（实测确认，见 F-008）。
    本函数把"填草案"这一步**提到 normalize 之前**，让修复真正发生。

    填入规则（草案是唯一权威来源，不做任何创作）：

    * ``reference_visual`` 可用（非空 str 且不超长）→ **原样不动**（零干预）；
    * 不可用 → 用 ``draft`` 替换；草案本身超长就按上限截断；
    * 草案也不可用（空/非 str）→ 不动，交给原生报错
      （此时确实没有任何可信画面依据，编造只会产出错画面）。
    """
    if not isinstance(value, dict):
        return value, ""
    current = value.get("reference_visual")
    usable = isinstance(current, str) and bool(current.strip()) \
        and len(current) <= _MAX_REFERENCE_VISUAL
    if usable:
        return value, ""

    text = str(draft or "").strip()
    if not text:
        # 草案也没有 ⇒ 不编造，交原生报它自己的错。
        return value, ""

    filled = text[:_MAX_REFERENCE_VISUAL].rstrip()
    why = "为空" if not isinstance(current, str) or not current.strip() else "过长"
    repaired = copy.deepcopy(value)
    repaired["reference_visual"] = filled
    note = (
        f"reference_visual {why}，已用本镜核心画面草案填入"
        f"（{len(filled)} 字符）"
        + ("；草案超长已按上限截断" if len(text) > _MAX_REFERENCE_VISUAL else "")
    )
    return repaired, note


def _clean_names(raw: Any) -> list[str] | None:
    """把主体名列表规整成「去重、去空、截断到上限的字符串列表」。

    返回 ``None`` 表示**无法规整**（不是 list）—— 那时交原生报错。
    与 ``video_prompt_refresh._clean_generated_motion_plan`` 的口径一致，
    本模块不复制它的实现、只复用同一套语义（去空/去重/去占位符）。
    """
    if not isinstance(raw, list):
        return None
    names: list[str] = []
    for item in raw:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            # 模型偶尔把主体写成 {"name": "..."} 对象（占位符形态）。
            name = item["name"].strip()
        else:
            # null / {} / 数字等 schema 占位符：没有可用身份，丢弃。
            continue
        if not name:
            continue
        name = name[:_MAX_NAME_LEN]
        if name not in names:
            names.append(name)
    return names[:_MAX_NAMES]


def _coerce_name_list(raw: Any) -> list[str] | None:
    """更宽的名字列表规整：额外容忍「该给列表却给了标量」的常见漂移。

    实测该模型会把这些字段写成 ``null``，或把列表写成一个字符串
    （``"林工、助手"``）。这类是**纯形状漂移，意图可还原**：

    * ``None`` / 空串 → ``[]``
      （契约原话：「无固定主体**可以为空**」⇒ 空是合法值，不是错误）
    * 字符串 → 按 `、,，;；换行` 拆成多个名字
      （模型把数组写成标量时的自然意图；单个名字自然得到单元素列表）
    * list → :func:`_clean_names`
    * 其它类型（dict/数字…）→ ``None``（无来源，交原生报错）
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        parts = [part.strip() for part in re.split(r"[、,，;；\n]", text) if part.strip()]
        names: list[str] = []
        for part in parts:
            name = part[:_MAX_NAME_LEN]
            if name and name not in names:
                names.append(name)
        return names[:_MAX_NAMES]
    return _clean_names(raw)


def repair_participants(value: Any) -> tuple[Any, str]:
    """规整 ``participants`` / ``reference_participants`` 的**形状**问题。

    修的是用户第三轮撞到的 ``核心参考图主体列表无效``（F-009）及其同类：

    | 原生失败 | 这里的处理 |
    |---|---|
    | ``participants`` 非 list（含 ``null``、标量字符串） | 规整（见 :func:`_coerce_name_list`） |
    | 条目含 ``null``/``{}``/数字等占位符 | 丢弃该条目（无可用身份） |
    | 条目是 ``{"name": "..."}`` | 取出 name |
    | 主体名重复 | 去重（先出现者优先） |
    | 超过 12 个 | 截断（保住前 12 个） |
    | 单个名字超 160 字符 | 截断 |
    | ``reference_participants`` 缺失/``null`` | **按本镜主体填入**（见下） |
    | ``reference_participants`` 非 ``participants`` 子集 | **补登记进 participants**（不删主体） |

    **关于「缺失就填 participants」**：契约要求 v2 必须有
    ``reference_participants``，但实测该模型经常整个不填（``None``）。
    此时没有任何可信来源能说出"哪几个主体真的画进了核心图"，
    于是取**保守全集**——即本镜主体全部视为画入核心图。理由三条：

    1. 契约本身写「核心图的主体场面优先接近视频前段」，取全集是它的自然读法；
    2. ``reference_texts`` 的 owner 校验是**按 reference_participants** 做的，
       填全集才不会把本来合法的文字归属判成非法；
    3. OCV 自己的口径就是"核心图里可见的主体必然是本镜主体"（
       ``repair_generated_participant_membership`` 的文档原话），方向是**补**而非删。

    ⚠ 绝不**凭空删除** reference_participants 里的主体来凑子集 ——
    那会静默丢掉画面里真实存在的人。
    """
    if not isinstance(value, dict):
        return value, ""
    notes: list[str] = []
    repaired = copy.deepcopy(value)

    raw_participants = repaired.get("participants")
    participants = _coerce_name_list(raw_participants)
    if participants is None:
        return value, ""       # 无来源可修（dict/数字等），交原生
    if participants != raw_participants:
        notes.append("participants 已规整（去空/去重/截断）")
    repaired["participants"] = participants

    # 只在 version=2 时才有 reference_participants（与原生契约一致）
    if repaired.get("version") != 2:
        return (repaired, "；".join(notes)) if notes else (value, "")

    raw_refs = repaired.get("reference_participants")
    if raw_refs is None:
        # 整个不填：取保守全集（理由见 docstring）
        references = list(participants)
        notes.append(
            f"reference_participants 缺失，已按本镜主体填入（{len(references)} 个，保守取全集）"
        )
    else:
        references = _coerce_name_list(raw_refs)
        if references is None:
            return (repaired, "；".join(notes)) if notes else (value, "")
        if references != raw_refs:
            notes.append("reference_participants 已规整（去空/去重/截断）")

    # 子集契约：把核心图里有、但未登记的主体补进 participants（不删任何主体）
    missing = [name for name in references if name not in participants]
    if missing:
        if len(participants) + len(missing) > _MAX_NAMES:
            # participants 已满：只补到上限（尽量保留，不越界）
            room = max(0, _MAX_NAMES - len(participants))
            for name in missing[:room]:
                participants.append(name)
            dropped = missing[room:]
            references = [name for name in references if name not in dropped]
            notes.append(
                f"核心图主体 {len(missing)} 个未登记，已补入 participants"
                f"（受 {_MAX_NAMES} 上限约束，{len(dropped)} 个无法保留）"
            )
        else:
            participants.extend(missing)
            notes.append(f"核心图主体 {'、'.join(missing)} 未登记，已补入 participants")

    repaired["participants"] = participants
    repaired["reference_participants"] = references
    if notes:
        return repaired, "；".join(dict.fromkeys(notes))
    return value, ""


def _clean_texts(raw: Any, owners: list[str], limit: int) -> list[dict[str, str]] | None:
    """规整一组文字记录。返回 None 表示无法规整（不是 list）。"""
    if not isinstance(raw, list):
        return None
    rows: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue                      # 占位符条目丢弃
        text = str(entry.get("text") or "").strip()[:_MAX_TEXT_LEN]
        owner = str(entry.get("owner") or "").strip()[:_MAX_NAME_LEN]
        container = str(entry.get("container") or "").strip()[:_MAX_TEXT_LEN]
        if not text or not owner or not container:
            continue                      # 三要素缺一不可，缺了就丢弃而不是猜
        row = {"text": text, "owner": owner, "container": container}
        if row not in rows:
            rows.append(row)
    return rows[:limit]


def _coerce_texts(raw: Any, limit: int) -> list[dict[str, str]] | None:
    """文字列表的宽规整：``None`` → ``[]``，其余交给 :func:`_clean_texts`。

    契约里「没有文字就填 ``[]``」，但模型经常整个不填（``None``）或漏写字段。
    `None` 与 `[]` 在语义上完全等价（都是"本阶段没有可见文字"），
    所以这是**纯形状规整**，不引入任何新语义。
    """
    if raw is None:
        return []
    return _clean_texts(raw, [], limit)


def repair_texts(value: Any) -> tuple[Any, str]:
    """规整 ``beats[].texts`` 与 ``reference_texts`` 的形状/重复/超限问题。

    | 原生失败 | 处理 |
    |---|---|
    | 短文字列表缺失/``null`` | 视为 ``[]``（契约：没有文字就填 ``[]``） |
    | 短文字列表非 list（标量等） | **不修**（交原生） |
    | 条目非对象 / 三要素缺失 | 丢弃该条目 |
    | 单条 text/container 超 80、owner 超 160 字符 | 截断 |
    | 同一条文字记录重复 | 去重 |
    | 条目数超上限（阶段 4 / 核心图 12） | 截断 |
    | ``owner`` 不在**适用的**主体列表且不是"画面标注" | **把 owner 补进那个列表** |

    ⚠ **两套 owner 校验用的主体列表不是同一个**（实测踩过）：
    ``beats[].texts`` 的 owner 对着 ``participants``，
    而 ``reference_texts`` 的 owner 对着 **``reference_participants``**
    （源码：``normalize_texts(value.get('reference_texts'),
    reference_participants, 12, '核心参考图')``）。
    只补 ``participants`` 会让 ``reference_texts`` 的归属仍然非法 ——
    必须补进**它自己那一套**列表里。

    补登记而非改归属的理由与 :func:`repair_participants` 相同：
    owner 是模型写下的归属，把承载文字的主体登记上，是修正记账漂移；
    改掉 owner 或删掉文字才是篡改语义（那些不做）。
    """
    if not isinstance(value, dict):
        return value, ""
    participants = _coerce_name_list(value.get("participants"))
    if participants is None:
        return value, ""
    reference_participants = _coerce_name_list(value.get("reference_participants"))
    repaired = copy.deepcopy(value)
    notes: list[str] = []

    # 两个"归属登记表"：阶段文字对 participants，核心图文字对 reference_participants。
    stage_added: list[str] = []
    reference_added: list[str] = []

    def ensure(store: list[str], owner: str, owners: list[str]) -> None:
        if owner and owner != _OWNER_LABEL and owner not in owners and owner not in store:
            store.append(owner)

    raw_beats = repaired.get("beats")
    if isinstance(raw_beats, list):
        for beat in raw_beats:
            if not isinstance(beat, dict):
                continue
            raw_texts = beat.get("texts")
            rows = _coerce_texts(raw_texts, 4)
            if rows is None:
                continue
            for row in rows:
                ensure(stage_added, row["owner"], participants)
            if rows != raw_texts:
                notes.append("阶段文字已规整")
            beat["texts"] = rows

    if repaired.get("version") == 2:
        raw_reference = repaired.get("reference_texts")
        rows = _coerce_texts(raw_reference, 12)
        if rows is not None:
            # reference_participants 缺失时，repair_participants 会把它填成
            # participants 的全集；这里若还拿不到（例如该函数被独立调用），
            # 退回 participants，保证 owner 有地方可登记。
            owners_for_reference = reference_participants if reference_participants is not None \
                else participants
            for row in rows:
                ensure(reference_added, row["owner"], owners_for_reference)
            if rows != raw_reference:
                notes.append("核心图文字已规整")
            repaired["reference_texts"] = rows

    # 分别补登记（各受 12 上限约束）
    if stage_added:
        room = max(0, _MAX_NAMES - len(participants))
        kept, dropped = stage_added[:room], stage_added[room:]
        participants.extend(kept)
        repaired["participants"] = participants
        if kept:
            notes.append(f"文字归属 {'、'.join(kept)} 未登记，已补入 participants")
        if dropped:
            for beat in repaired.get("beats") or []:
                if isinstance(beat, dict) and isinstance(beat.get("texts"), list):
                    beat["texts"] = [r for r in beat["texts"] if r.get("owner") not in dropped]
            notes.append(f"主体数已达上限，{len(dropped)} 个无归属文字条目已丢弃")

    if reference_added and repaired.get("version") == 2:
        base_owners = reference_participants if reference_participants is not None else participants
        room = max(0, _MAX_NAMES - len(base_owners))
        kept, dropped = reference_added[:room], reference_added[room:]
        base_owners.extend(kept)
        repaired["reference_participants"] = base_owners
        if kept:
            notes.append(f"核心图文字归属 {'、'.join(kept)} 未登记，已补入 reference_participants")
        if dropped:
            if isinstance(repaired.get("reference_texts"), list):
                repaired["reference_texts"] = [
                    r for r in repaired["reference_texts"] if r.get("owner") not in dropped]
            notes.append(f"核心图主体数已达上限，{len(dropped)} 个无归属文字条目已丢弃")
        # 补进去的主体也必须满足"reference_participants ⊆ participants"
        extra = [name for name in repaired["reference_participants"] if name not in repaired["participants"]]
        if extra:
            repaired["participants"] = repaired["participants"] + extra

    if notes:
        return repaired, "；".join(dict.fromkeys(notes))
    return value, ""


def repair_beat_prompts(plan: Any, row: Any) -> tuple[Any, str]:
    """规整 ``beat_prompts`` / ``continuity_prompt`` / ``ending_prompt`` 的形状（F-010）。

    修的是用户第四轮报错 —— **六个镜头全部**报同一条：

        提示词与阶段方案核对失败：<id> 缺少逐阶段定稿段落；...（×6）

    六个全中 ⇒ 系统性形状漂移，不是偶发。原生 ``_assemble_video_body``
    对这三个字段有三道硬校验，逐条对应处理：

    | 原生失败 | 成因 | 处理 |
    |---|---|---|
    | ``缺少逐阶段定稿段落`` | ``beat_prompts`` 非 list，或元素非 dict | **字符串列表按序包成 ``{beat:i, prompt:s}``**；整个缺失则按阶段数回落 ``action``；真·标量则不修 |
    | ``定稿阶段缺失、重复或顺序改变`` | ``beat`` 号非 int 或 ≠ ``[1..N]`` | **按位置重编号**（契约明写"按 beats 原顺序一一对应"⇒ 位置即编号） |
    | ``场景、阶段或结尾定稿为空`` | 有空串 | 阶段正文为空 ⇒ 回落该阶段 ``action``；``continuity_prompt`` ⇒ 回落 ``scene_anchor``；``ending_prompt`` ⇒ 回落**末阶段 action** |

    ## 为什么"回落"不是编造

    三个兜底来源**全部取自同一份 ``motion_plan``**，是 Agent 3 已经定稿的
    权威内容：

    * 阶段正文 ← ``beats[i].action``
    * ``continuity_prompt``（造型/场景保持）← ``scene_anchor``
      （方案自己写的"稳定场景与空间关系"）
    * ``ending_prompt``（结束状态）← **末阶段 ``action``**

    本函数的职责本来就是"把方案整理成提示词"；整理者（Agent 5）漏写某段时，
    用**同一份方案里已有的描述**补上，是"退回权威来源"，不是凭空创作。
    反之若某阶段连 ``action`` 都为空，**不编造** —— 交给原生报错。

    > 注：实测 ``continuity_prompt`` / ``ending_prompt`` **键整个缺失也报错**，
    > 所以"直接省略"行不通，必须给出内容。这正是必须回落而非删除的原因。
    """
    if not isinstance(row, dict) or not isinstance(plan, dict):
        return row, ""
    if plan.get("version") != 2:
        return row, ""
    beats = plan.get("beats")
    if not isinstance(beats, list) or not beats:
        return row, ""

    total = len(beats)
    notes: list[str] = []
    repaired = copy.deepcopy(row)

    # ---- ① beat_prompts 的形状 ----
    raw = repaired.get("beat_prompts")
    if isinstance(raw, list) and raw and all(isinstance(p, str) for p in raw):
        # 模型把每段写成字符串列表：顺序即阶段号（契约要求的顺序）
        parts = [{"beat": index, "prompt": text} for index, text in enumerate(raw, 1)]
        notes.append("beat_prompts 是字符串列表，已按顺序编号")
    elif isinstance(raw, list):
        # {beat,prompt} 形态（可能混入占位符），交给下面按位重编号
        parts = raw
    elif raw is None or raw == "" or raw == []:
        # **整个字段缺失**：阶段数已知（=len(beats)），逐个回落方案 action。
        # 这是"形状缺失 + 有权威来源"，可还原 ⇒ 修，而不是放弃。
        parts = []
        notes.append(f"beat_prompts 缺失，已按 {total} 个阶段回落方案描述")
    else:
        # 真·标量/字典等：无可靠位置可推断，且**一个阶段都没有** ⇒ 交原生
        return row, ""

    # ---- ② 按位置重编号（契约：顺序即编号）----
    if len(parts) > total:
        parts = parts[:total]
        notes.append(f"beat_prompts 段落数超过阶段数，已截断到 {total}")
    # 逐段规整正文
    normalized: list[dict[str, Any]] = []
    for index, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            part = {"prompt": part}
        body = part.get("prompt")
        if not isinstance(body, str) or not body.strip():
            # 回落该阶段的权威描述（Agent 3 的 action）
            beat = beats[index - 1] if isinstance(beats[index - 1], dict) else {}
            fallback = beat.get("action")
            if isinstance(fallback, str) and fallback.strip():
                body = fallback
                notes.append(f"第 {index} 阶段正文为空，已回落该阶段方案描述")
            else:
                body = ""
        if type(part.get("beat")) is not int or part.get("beat") != index:
            if part.get("beat") != index:
                notes.append("beat_prompts 编号已按顺序规整")
        normalized.append({"beat": index, "prompt": body if isinstance(body, str) else ""})
    # 段落数不足阶段数 ⇒ 缺的用方案 action 补（同上，权威来源）
    while len(normalized) < total:
        beat = beats[len(normalized)] if isinstance(beats[len(normalized)], dict) else {}
        fallback = beat.get("action")
        normalized.append({
            "beat": len(normalized) + 1,
            "prompt": fallback if isinstance(fallback, str) else "",
        })
        notes.append(f"第 {len(normalized)} 阶段缺失，已回落该阶段方案描述")

    # 空正文仍然无解（连 action 都空）⇒ 不编造，交原生报错。
    # ⚠ 这里**只放弃阶段正文**，不再提前 return ——
    #   否则框架文字的兜底永远执行不到
    #   （实测踩过：ending_prompt=None 的场景因此未被修好）。
    stage_ok = not any(not str(part["prompt"]).strip() for part in normalized)
    if stage_ok:
        repaired["beat_prompts"] = normalized

    # ---- ③ 框架文字：用**方案里的权威内容**兜底 ----
    #
    # 原生要求 ``continuity_prompt`` 与 ``ending_prompt`` **必须存在且非空**
    # （实测：键整个缺失也报同一个错）。所以"直接省略"是行不通的 ——
    # 必须给出内容。所幸 motion_plan 里有**权威来源**可用，不需要编造：
    #
    #   * ``continuity_prompt``（本镜造型/场景保持要求）
    #     ← ``plan['scene_anchor']``（方案自己写的稳定场景与空间关系）
    #   * ``ending_prompt``（结束状态与尾部停留）
    #     ← 最后一个阶段的 ``action``（方案自己写的结束状态），
    #       再补一句"余下时间自然停留"（这是**固定的接口约束**，
    #       不是对画面内容的创作 —— 契约原文就要求 generation_duration
    #       多出的部分自然停留）。
    scene_anchor = plan.get("scene_anchor")
    last_action = ""
    for beat in reversed(beats):
        if isinstance(beat, dict) and isinstance(beat.get("action"), str) and beat["action"].strip():
            last_action = beat["action"].strip()
            break

    for key, fallback, label in (
        ("continuity_prompt", scene_anchor, "场景基础"),
        ("ending_prompt", last_action, "末阶段描述"),
    ):
        value = repaired.get(key)
        if isinstance(value, str) and value.strip():
            continue
        if isinstance(fallback, str) and fallback.strip():
            repaired[key] = fallback.strip()
            notes.append(f"{key} 为空，已用方案的{label}兜底")

    if not stage_ok:
        return repaired, "；".join(dict.fromkeys(notes)) if notes else ""

    if notes:
        return repaired, "；".join(dict.fromkeys(notes))
    if repaired.get("beat_prompts") != row.get("beat_prompts"):
        return repaired, "beat_prompts 已按阶段顺序规整"
    return row, ""


def _make_assemble_wrapper() -> Any:
    """包装 ``_assemble_video_body``：在原生校验之前把 row 规整好（F-010）。

    ⚠ **必须原地修改调用方那个 ``row`` 对象**，不能只返回一个副本：
    ``_finalize_prompts`` 拿到 row 后 ``assemble(shot, row)``，
    紧接着读 ``row.get(field)`` —— 它读的是**同一个对象**。
    若包装层换成副本，原生函数写进副本的 ``video_prompt`` 就丢了，
    调用方会拿到空提示词 ⇒ 报出"缺少最终提示词"这种**误导性**错误。
    """
    original = _ORIGINAL_ASSEMBLE

    def _resilient_assemble_video_body(shot: Any, row: Any) -> Any:
        if enabled() and isinstance(shot, dict) and isinstance(row, dict):
            repaired, note = repair_beat_prompts(shot.get("motion_plan"), row)
            if note:
                _REPAIRED.append({"note": note, "field": "_assemble_video_body"})
                _log(f"视频提示词组装：{note}（已规整后交原生校验，不再终止任务）")
                # 原地替换，保住对象身份（见 docstring）
                row.clear()
                row.update(repaired)
        return original(shot, row)

    _resilient_assemble_video_body._cloud_stack_wrapped = True  # type: ignore[attr-defined]
    return _resilient_assemble_video_body


def _make_wrapper() -> Any:
    original = _ORIGINAL

    def _resilient_normalize_motion_plan(value: Any) -> Any:
        if enabled():
            # 顺序有意：先修主体/文字记账（participants 决定 texts 的 owner 合法性），
            # 再修 reference_beat（它只依赖 beats 的**数量**），最后修 reference_visual。
            # 每个 repair 都是「无需改动就原样返回」，因此顺序不影响正确性。
            for repair in (repair_participants, repair_texts,
                           repair_reference_beat, repair_reference_visual):
                repaired, note = repair(value)
                if note:
                    _REPAIRED.append({"note": note, "field": repair.__name__})
                    _log(f"动态阶段方案：{note}（已修复后交原生校验，不再终止任务）")
                    value = repaired
        return original(value)

    _resilient_normalize_motion_plan._cloud_stack_wrapped = True  # type: ignore[attr-defined]
    return _resilient_normalize_motion_plan


def _make_restore_wrapper() -> Any:
    original = _ORIGINAL_RESTORE

    def _resilient_restore_reference_draft(plan: Any, visual_description: Any) -> Any:
        if enabled():
            repaired, note = repair_reference_visual_with_draft(plan, visual_description)
            if note:
                _REPAIRED.append({"note": note, "field": "reference_visual(draft)"})
                _log(f"动态阶段方案：{note}（已在原生 normalize 之前修好，修复路径可达）")
                plan = repaired
        return original(plan, visual_description)

    _resilient_restore_reference_draft._cloud_stack_wrapped = True  # type: ignore[attr-defined]
    return _resilient_restore_reference_draft


def install_on(module: Any) -> bool:
    """把 ``module`` 里的目标函数换成包装版（幂等）。

    返回是否**本次发生了替换**。首次调用会记下真正的原生函数；之后所有
    目标模块共用同一个 wrapper（wrapper 内部只调那一个原生实现）。

    三个函数都要换：

    1. ``normalize_motion_plan`` —— 字段记账（F-007/8/9）；
    2. ``restore_reference_draft`` —— F-008 的"修复路径不可达"：它先 normalize
       再修 ``reference_visual``，空/超长时在 normalize 那步就抛了，
       必须在**它之前**把草案填进去；
    3. ``_assemble_video_body`` —— F-010：它在拼装前对 ``beat_prompts`` 做硬校验，
       形状漂移时直接抛错，必须在**它之前**把 row 规整好。

    ## ⚠ 为什么缺属性时必须**记下来待重试**（本模块踩过的最隐蔽的坑）

    目标函数在模块里是**按定义顺序**陆续出现的：``video_agents`` 的模块体
    先执行到 ``def write_video_prompts``，之后再执行 ``def _assemble_video_body``。
    而补丁的触发时机是"上游模块 ``video_motion_plan`` 执行完"——那一刻
    ``video_agents`` 可能**还没定义出** ``_assemble_video_body``。

    此时 ``getattr(module, name, None)`` 返回 ``None``。如果只是"这轮跳过"，
    **之后再没有任何时机来补**（补丁只触发一次）⇒ ``_assemble_video_body``
    永远是原生版，本补丁形同不存在。

    实测踩到：全链路跑通时它在 Agent 5 那步仍然报
    ``缺少逐阶段定稿段落``，而单元测试全绿 —— 因为单元测试里
    ``video_agents`` 早已完整导入。

    **修法**：把"属性还没出现"的模块登记进 ``_PENDING``，由
    :func:`retry_pending` 在后续 import 时反复尝试（有上限，避免永久轮询）。
    这样无论谁先谁后，最终都会被装上。
    """
    global _INSTALLED, _ORIGINAL, _ORIGINAL_RESTORE, _ORIGINAL_ASSEMBLE
    global _WRAPPER, _WRAPPER_RESTORE, _WRAPPER_ASSEMBLE
    changed = False

    targets = (
        ("normalize_motion_plan", "_ORIGINAL", "_WRAPPER", "_make_wrapper"),
        ("restore_reference_draft", "_ORIGINAL_RESTORE", "_WRAPPER_RESTORE", "_make_restore_wrapper"),
        ("_assemble_video_body", "_ORIGINAL_ASSEMBLE", "_WRAPPER_ASSEMBLE", "_make_assemble_wrapper"),
    )
    module_name = getattr(module, "__name__", "")
    for attr, orig_name, wrap_name, maker_name in targets:
        current = getattr(module, attr, None)
        if current is None:
            # 属性还没定义出来（模块体还在往下执行）。
            if attr != "_assemble_video_body":
                continue
            # ⚠ 只对**真正拥有这个函数的模块**登记待重试。
            #   `_assemble_video_body` 是 video_agents 的私有函数，
            #   video_motion_plan / video_plan 根本没有它 —— 对它们
            #   无脑登记会让重试计数被耗光（实测：pending 里
            #   video_motion_plan=65、video_plan=66，而 video_agents
            #   反而没被登记，包装永远装不上）。
            if module_name != "backend.app.video_agents":
                continue
            with _PENDING_LOCK:
                _PENDING[module_name] = _PENDING.get(module_name, 0) + 1
            continue
        wrapper = globals()[wrap_name]
        if current is wrapper or getattr(current, "_cloud_stack_wrapped", False):
            continue
        if globals()[orig_name] is None:
            globals()[orig_name] = current
        if wrapper is None:
            wrapper = globals()[maker_name]()
            globals()[wrap_name] = wrapper
        setattr(module, attr, wrapper)
        with _PENDING_LOCK:
            _PENDING.pop(module_name, None)
        changed = True

    if changed:
        _INSTALLED = True
    return changed


def retry_pending() -> bool:
    """重试那些"属性当时还不存在"的目标模块（供 import 钩子调用）。

    这是 F-010 能真正生效的关键：``_assemble_video_body`` 在
    ``video_agents`` 模块体靠后位置定义，而补丁由上游模块触发，
    首次必然拿不到。没有这个重试，包装永远装不上。
    """
    # 本模块自己可能还在被导入（on_import 回调触发）——
    # 此时 _PENDING 都还没定义，直接返回。
    pending = globals().get("_PENDING")
    if not pending:
        return False
    with _PENDING_LOCK:
        targets = [name for name, count in pending.items() if count <= _RETRY_LIMIT]
    if not targets:
        return False
    changed = False
    for name in targets:
        module = sys.modules.get(name)
        if module is None:
            continue
        # 模块体还在执行时不动它：此刻改的属性会被后面的 def 覆盖回去。
        spec = getattr(module, "__spec__", None)
        if getattr(spec, "_initializing", False):
            with _PENDING_LOCK:
                pending[name] = pending.get(name, 0) + 1
            continue
        if install_on(module):
            changed = True
    return changed


def install(*modules: Any) -> bool:
    """把补丁装到指定的模块上；不传则装到所有已导入的目标模块。

    之所以要"所有目标模块"，见模块文档的 ``from ... import`` 绑定陷阱。
    """
    changed = False
    if modules:
        for module in modules:
            changed = install_on(module) or changed
    else:
        for name in _TARGET_MODULES:
            module = sys.modules.get(name)
            if module is not None:
                changed = install_on(module) or changed
    if _INSTALLED and changed:
        _log(
            "已安装动态阶段方案健壮性补丁："
            "① reference_beat 越界/类型漂移夹紧；"
            "② reference_visual 为空/超长用草案填入或截断；"
            "③ 主体名/文字记录的形状·重复·超限·归属未登记规整；"
            "④ beat_prompts 形状漂移按顺序规整并回落方案描述 —— "
            "不再以阶段编号/参考画面/主体列表/逐阶段定稿段落等记账问题终止动态视频生产"
        )
    return changed


def installed() -> bool:
    return _INSTALLED


def diagnostics() -> dict[str, Any]:
    """修复统计，供自检与排查读取（不做静默修复）。

    ⚠ 判断"某模块是否已包装"必须**先要求 wrapper 非 None**：
    ``getattr(mod, name, None) is _WRAPPER_X`` 在 ``_WRAPPER_X`` 为 ``None`` 时，
    对**没有该属性**的模块也成立（``None is None``）⇒ 会把一堆无关模块
    误报成"已包装"。实测踩到：``wrapped_assemble`` 曾列出
    video_motion_plan 等根本没有该函数的模块，反而漏掉了 video_agents。
    """
    def wrapped(attr: str, wrapper: Any) -> list[str]:
        if wrapper is None:
            return []
        return [name for name in _TARGET_MODULES
                if getattr(sys.modules.get(name), attr, None) is wrapper]

    return {
        "enabled": enabled(),
        "installed": _INSTALLED,
        "repaired_count": len(_REPAIRED),
        "repaired": list(_REPAIRED[-20:]),
        "wrapped_modules": wrapped("normalize_motion_plan", _WRAPPER),
        "wrapped_restore": wrapped("restore_reference_draft", _WRAPPER_RESTORE),
        "wrapped_assemble": wrapped("_assemble_video_body", _WRAPPER_ASSEMBLE),
        "pending": dict(_PENDING),
    }


def uninstall(*modules: Any) -> bool:
    """撤掉包装，恢复原生行为（自检的对照组用）。"""
    global _INSTALLED
    if _ORIGINAL is None and _ORIGINAL_RESTORE is None and _ORIGINAL_ASSEMBLE is None:
        return False
    targets = modules or tuple(
        module for module in (sys.modules.get(name) for name in _TARGET_MODULES)
        if module is not None
    )
    restored = False
    for module in targets:
        if _WRAPPER is not None and getattr(module, "normalize_motion_plan", None) is _WRAPPER:
            module.normalize_motion_plan = _ORIGINAL  # type: ignore[attr-defined]
            restored = True
        if _WRAPPER_RESTORE is not None and getattr(module, "restore_reference_draft", None) is _WRAPPER_RESTORE:
            module.restore_reference_draft = _ORIGINAL_RESTORE  # type: ignore[attr-defined]
            restored = True
        if _WRAPPER_ASSEMBLE is not None and getattr(module, "_assemble_video_body", None) is _WRAPPER_ASSEMBLE:
            module._assemble_video_body = _ORIGINAL_ASSEMBLE  # type: ignore[attr-defined]
            restored = True
    _INSTALLED = False
    return restored
