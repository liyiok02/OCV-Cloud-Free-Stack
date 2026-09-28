# SPDX-License-Identifier: AGPL-3.0-only
"""语言模型（LLM）请求的 30 档阶梯重试。

## 为什么需要

OCV 原生的 ``GEMINI_RETRY_COUNT`` 默认只有 **3**，且间隔是"3s 起、指数、
30s 封顶"。实测遇到 429（tpm/rpm 限流）时经常熬不过去。

最初的实现对齐 ``chai1110/dsh-provider-config`` 的推荐策略
（``maxRetries: 15``、指数退避 1→2→4→8→16→30）。但实测日志显示
（F-016）：**15 次确实跑满了，却从第 6 次起就顶在 30s**，后 10 次只是
原地重复，合计仅 5.8 分钟 —— 而 429 的 tpm 窗口通常是**分钟级**，
30s 封顶把大部分重试预算浪费掉了。

那份参考文档面向的是**单轮对话**；OCV 的 Agent 1 是一次长规划调用、
背后是共享配额，需要**更长、更粗的尾档**。所以改为与云端视频
**完全共用**同一张 30 档表（用户要求"和视频机制类似"）：

    1-3   次每 1 秒
    4-8   次每 5 秒
    9-12  次每 10 秒
    13-15 次每 15 秒
    16-20 次每 30 秒
    21-25 次每 45 秒
    26-30 次每 1 分钟
    合计 788 秒 ≈ 13.1 分钟

## 挂点：复用 OCV 自己的重试循环，**不新增循环**

这是本模块最重要的设计决定。

OCV ``gemini_client`` 已经在每一条生成路径里写了重试循环::

    for attempt in range(1, _gemini_retry_count() + 1):
        ...
        if response.status_code in RETRYABLE_STATUS_CODES and attempt < _gemini_retry_count():
            time.sleep(_gemini_retry_delay(attempt))

所以**正确的做法是包装它的两个策略函数**，而不是在 ``generate_gemini_text``
外面再套一层重试。后者会与内层循环相乘（3 × 31 = 93 次请求），是典型的
"重试放大"事故。

``_gemini_retry_count`` / ``_gemini_retry_delay`` 在整个仓库里**只被
``gemini_client`` 自己以模块级名字调用**（无任何 ``from ... import`` 绑定），
因此替换模块属性即可全量生效 —— 天然规避铁律 29 的 from-import 绑定陷阱。

## 作用域：只对本插件注册的 provider 生效

``_generate_openai_compatible_text`` 同时服务多个 provider（deepseek / openai /
qwen / kimi / glm…）。若不设作用域，用户把 ``LANGUAGE_PROVIDER`` 切到别家时
也会被套上 30 次重试 —— 那不是用户要的。所以每次调用都实时检查
``_provider()`` 是否等于本插件注册的 provider id。

## 序号语义（铁律 53：累计尝试 ≠ 第几次重试 ≠ 上限）

* 面板/配置里的 ``CLOUD_STACK_LLM_RETRY_COUNT`` = **重试次数**（默认 30）。
* ``_gemini_retry_count()`` 的返回值 = **总尝试次数** = 重试次数 + 1（默认 31）。

两者差一，是这台机器上已经踩过的坑（见 F-013 后续调整），因此这里显式
区分命名：:func:`retries` 与 :func:`attempt_budget`。
"""

from __future__ import annotations

import os
import random
import threading
from typing import Any

from . import config

_LOG_PREFIX = "[cloud_free_stack]"

# 与本插件在 gemini_client.LANGUAGE_PROVIDER_OPTIONS 里注册的 provider id 一致。
# 只有当前选中的 provider 是它时，本模块才接管重试策略。
PLUGIN_PROVIDER = "sensenova"

# ---------------------------------------------------------------------------
# 退避档位：**与视频完全共用同一张表**（config.LADDER_RETRY_TIERS）
#
# 用户明确要求「改成和视频机制类似的 30 档位」（2026-09-25，F-016）。
# 表本身放在 config 里做单一真相源，这里只做转发 —— 各写一份必然漂移，
# 本仓库已为"两份清单漂移"付过代价（F-006）。
#
# 档位（合计 788 秒 ≈ 13.1 分钟）：
#     1-3   次每 1 秒
#     4-8   次每 5 秒
#     9-12  次每 10 秒
#     13-15 次每 15 秒
#     16-20 次每 30 秒
#     21-25 次每 45 秒
#     26-30 次每 1 分钟
# ---------------------------------------------------------------------------

# 默认重试次数：与视频默认一致（30）
DEFAULT_RETRIES = 30
MAX_RETRIES = 60

# 抖动比例。视频那条路是**固定间隔不抖动**（它靠后台线程按绝对时刻调度）；
# 语言模型这条是**同步 sleep**，多个阶段进程可能同时撞限流，所以保留抖动
# 以避免惊群。比例取参考文档 dsh-provider-config 的 0.3。
JITTER_RATIO = 0.3

_STATS_LOCK = threading.Lock()
_STATS: dict[str, int] = {"retries": 0, "calls": 0}


def _log(message: str) -> None:
    try:
        print(f"{_LOG_PREFIX} {message}", flush=True)
    except Exception:  # noqa: BLE001 - 日志永远不能影响模型调用
        pass


def _debug(message: str) -> None:
    try:
        if config.debug():
            _log(message)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 开关与档位
# ---------------------------------------------------------------------------


def _flag(key: str, *, default: bool) -> bool:
    """读布尔开关：``os.environ`` 优先，再看面板层。

    与 ``agent1b_resilience._flag`` 同一条设计原则 —— 面板保存时
    ``bootstrap.export_store_to_environ()`` 会把非空值写进 ``os.environ``，
    所以两种来源都能生效；而 ``os.environ`` 是排查者唯一能即时设置的地方。
    """
    value = str(os.environ.get(key) or "").strip()
    if not value:
        value = str(config.get(key) or "").strip()
    if not value:
        return default
    return value.lower() not in {"0", "false", "no", "off"}


def enabled() -> bool:
    """阶梯重试是否生效。

    默认**开启**：这是用户明确要求增加的能力，且只在"上游明确可重试"
    （429 / 5xx / 超时）时才起作用 —— 鉴权与参数类错误照旧立刻失败，
    不会因为开了它而空转（见 :func:`retryable_status`）。
    """
    return _flag("CLOUD_STACK_LLM_RETRY_ENABLED", default=True)


def retries() -> int:
    """最多**重试**多少次（不含首次请求）。默认 30（与视频一致）。"""
    return max(0, min(MAX_RETRIES, config.get_int("CLOUD_STACK_LLM_RETRY_COUNT", DEFAULT_RETRIES)))


def attempt_budget() -> int:
    """交给 OCV ``_gemini_retry_count()`` 的**总尝试次数** = 重试次数 + 1。

    铁律 53：OCV 的循环是 ``range(1, N + 1)`` 且以 ``attempt < N`` 判断是否
    再重试，因此 ``N`` 是总尝试次数，重试发生在第 1..N-1 次之后。
    要让"30 次重试"真的成立，这里必须返回 31。
    """
    return retries() + 1


def base_delay(retry_ordinal: int) -> float:
    """第 ``retry_ordinal`` 次重试前的**未加抖动**秒数（序号从 1 开始）。

    直接委托给共用的档位表 :func:`config.ladder_retry_delay` ——
    与视频**逐项相同**。
    """
    return config.ladder_retry_delay(retry_ordinal)


def delay(retry_ordinal: int) -> float:
    """实际等待秒数 = :func:`base_delay` 叠加 ±30% 抖动。

    抖动的用途是"避免惊群"：多个并发阶段进程同时撞上限流时，不抖动的相同
    退避会让它们在同一毫秒一起重发，把限流变成持续拥塞。
    """
    value = base_delay(retry_ordinal)
    if value <= 0:
        return 0.0
    return max(0.0, value * random.uniform(1.0 - JITTER_RATIO, 1.0 + JITTER_RATIO))


def total_window(count: int | None = None) -> float:
    """跑完全部重试的**未加抖动**总等待秒数（排障与面板提示用）。

    ``count`` 缺省时用当前配置值；显式传入可对内置默认做断言（铁律 32）。
    """
    total = retries() if count is None else max(0, int(count))
    return config.ladder_retry_window(total)


def schedule_text(count: int | None = None) -> str:
    """给人看的档位说明。

    ``count`` 目前只用于"是否为 0"的判断（档位表本身与次数无关），
    保留参数是为了让调用方语义清晰、并兼容既有断言。
    """
    total = retries() if count is None else max(0, int(count))
    if total <= 0:
        return "不重试（保持 OCV 原生行为）"
    return config.ladder_retry_schedule()


# ---------------------------------------------------------------------------
# 可重试判定
# ---------------------------------------------------------------------------

# 与 OCV gemini_client.RETRYABLE_STATUS_CODES 对齐：408/409/429/5xx。
# 这里只用于**文档与自检**（真正的判定在 OCV 的循环里做），但显式列出来能让
# "哪些错误会重试"这件事可被测试断言，而不是散落在上游源码里。
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


def retryable_status(status: int) -> bool:
    """该 HTTP 状态是否属于"上游明确可重试"。"""
    return int(status) in RETRYABLE_STATUS


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------


def in_scope(module: Any) -> bool:
    """当前选中的 provider 是否就是本插件注册的那一个。"""
    try:
        provider = str(module._provider() or "").strip().lower()
    except Exception:  # noqa: BLE001 - 取不到就当作不在作用域，保持原生行为
        return False
    return provider == PLUGIN_PROVIDER


def install(module: Any) -> bool:
    """把 30 档阶梯重试接到 ``module``（即 ``backend.app.gemini_client``）上。

    幂等；只替换 ``_gemini_retry_count`` / ``_gemini_retry_delay`` 两个模块属性，
    **不动**重试循环本身。
    """
    if getattr(module, "_cloud_stack_llm_retry", False):
        return True

    original_count = getattr(module, "_gemini_retry_count", None)
    original_delay = getattr(module, "_gemini_retry_delay", None)
    if not callable(original_count) or not callable(original_delay):
        return False

    def _count_with_retry() -> int:
        """总尝试次数。不在作用域或开关关闭时，原样返回 OCV 的值。"""
        try:
            if not enabled() or not in_scope(module):
                return int(original_count())
        except Exception:  # noqa: BLE001 - 策略函数绝不能抛
            return int(original_count())
        with _STATS_LOCK:
            _STATS["calls"] += 1
        return attempt_budget()

    def _delay_with_retry(attempt: Any) -> float:
        """等待秒数。OCV 只在"确实要再重试一次"时才调用本函数。"""
        try:
            if not enabled() or not in_scope(module):
                return float(original_delay(attempt))
        except Exception:  # noqa: BLE001
            return float(original_delay(attempt))
        seconds = delay(attempt)
        with _STATS_LOCK:
            _STATS["retries"] += 1
            ordinal = _STATS["retries"]
        _log(
            f"语言模型限流/暂时性失败，第 {ordinal} 次阶梯重试："
            f"等待 {seconds:.1f}s 后重发（上限 {retries()} 次，档位 {schedule_text()}）"
        )
        return seconds

    _count_with_retry._cloud_stack_llm_retry_wrapper = True  # type: ignore[attr-defined]
    _delay_with_retry._cloud_stack_llm_retry_wrapper = True  # type: ignore[attr-defined]

    module._gemini_retry_count = _count_with_retry  # type: ignore[attr-defined]
    module._gemini_retry_delay = _delay_with_retry  # type: ignore[attr-defined]
    module._cloud_stack_llm_retry = True  # type: ignore[attr-defined]
    module._cloud_stack_llm_retry_original = (original_count, original_delay)  # type: ignore[attr-defined]

    _log(
        f"已接管语言模型重试策略：{retries()} 次阶梯退避"
        f"（档位 {schedule_text()}，合计约 {total_window():.0f}s）"
        f"，可经面板「语言模型重试」开关关闭"
    )
    return True


def uninstall(module: Any) -> bool:
    """还原 OCV 原生重试策略（自检的负向对照用）。"""
    pair = getattr(module, "_cloud_stack_llm_retry_original", None)
    if not isinstance(pair, tuple) or len(pair) != 2:
        return False
    module._gemini_retry_count = pair[0]  # type: ignore[attr-defined]
    module._gemini_retry_delay = pair[1]  # type: ignore[attr-defined]
    for name in ("_cloud_stack_llm_retry", "_cloud_stack_llm_retry_original"):
        if hasattr(module, name):
            delattr(module, name)
    return True


def installed() -> bool:
    """补丁是否已装上（不导入 gemini_client 的轻量判定）。"""
    import sys

    module = sys.modules.get("backend.app.gemini_client")
    if module is None:
        return False
    return bool(getattr(module, "_cloud_stack_llm_retry", False))


def stats() -> dict[str, int]:
    """本进程内的调用/重试计数（排障用）。"""
    with _STATS_LOCK:
        return dict(_STATS)


def reset_stats() -> None:
    with _STATS_LOCK:
        _STATS["retries"] = 0
        _STATS["calls"] = 0
