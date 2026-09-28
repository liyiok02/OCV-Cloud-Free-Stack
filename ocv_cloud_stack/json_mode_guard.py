"""保证 ``response_format: json_object`` 的请求满足 OpenAI 契约。

## 症状

Agent 1 在任何模型调用之前终止，报：

    原始错误：OpenAI 兼容接口（云免费栈） 调用失败: <model>:
    HTTP 400 Prompt must contain the word 'json' in some form to use
    'response_format' of type 'json_object'.

## 根因（一阶：不是 Key、不是余额、不是限流）

``backend/app/gemini_client.py`` 的 ``_generate_openai_compatible_text`` 里：

    if response_mime_type == "application/json" and json_root != "array":
        extra_body["response_format"] = {"type": "json_object"}

而 ``story_agents.py`` 里**三处** json 调用点都没有传 ``json_root``：

* ``:1449``  ``_create_timeline_story_plan``   —— Agent 1 主路径
* ``:1624``  ``create_story_plan``             —— Agent 1 另一条路径
* ``:997``   Agent 1B 边界细化
* ``:1882``  ``_create_segment_story_plan``    —— 长文分段

所以 ``json_object`` 必然被发出。OpenAI 与 DeepSeek 的硬契约是：一旦声明
``json_object``，``messages`` 全文里必须出现字面量 "json"（不区分大小写），
否则直接 400 —— 服务端**不会**去看你的提示词写得多么明确。

## 根因（二阶：为什么出厂配置一直没事）

出厂提示词里恰好写着：

    TIMELINE_AGENT_SYSTEM_PROMPT:        只输出严格 JSON 对象：{...}
    STORY_AGENT_SYSTEM_PROMPT / GENERAL / SCIENCE: 同样含 "JSON"
    AGENT1B_BOUNDARY_REFINER_PROMPT:     含 "JSON"

于是"能跑"这件事**完全依赖提示词里恰好出现的这四个字母**。一旦用户设置
``AGENT1_PROMPT_SYSTEM`` 且该文本**不含** "json"，而它又命中
``"semantic_units" in custom_prompt`` 分支（亦即用户在自定义里提到了
semantic_units 这个字段名 —— 这正是照着官方 Agent 1 提示词微调时会做的事），
``story_agents.py:1436`` 就不会把出厂提示词追加回来：

    if custom_prompt and "semantic_units" not in custom_prompt:
        system_prompt += "\\n\\n" + TIMELINE_AGENT_SYSTEM_PROMPT

系统提示被**整体替换**成一个不含 json 的版本 ⇒ 缓存指纹变化（指纹包含
AGENT1_PROMPT_SYSTEM 的 sha1）⇒ 重新规划 ⇒ 400。

二阶结论：**这不是随机故障，是配置组合触发的确定性故障**。命中的配置是
「自定义 Agent 1 提示词 + 该提示词含 semantic_units + 该提示词不含 json」。
``.env`` / 面板层里目前都没有 ``AGENT1_PROMPT_SYSTEM``，说明它来自
``backend/app/main.py`` 的请求字段 ``agent1_prompt_system``（随任务提交，
写进 ``os.environ``），属于**跨次残留**：一次设置之后，之后每个任务都会带上。

## 修复

不碰 OCV 源码，改在 ``requests.sessions.Session.request`` 这一层做**范围严格
受限**的兜底：仅当

1. URL 以面板配置的语言模型地址开头；
2. 请求体是带 ``messages`` 的 dict；
3. ``response_format == {"type": "json_object"}"``；
4. ``messages`` 全文（含 role）**不含** "json"

时，给 user 消息补一行要求以 JSON 对象作答的指令。四个条件同时成立才动手，
所以这个补丁对**出厂提示词完全透明**（第 4 条不成立），也不会碰到图片请求
（无 ``messages``）或 TTS（走本地 shim）。

## 为什么打在这里

* 打 ``requests.post`` 是**无效**的：``_install_llm_body_injector`` 装注入器时
  把 ``original_post`` 捕获进了闭包，之后再替换 ``requests.post`` 只影响新调用。
  打 ``Session.request`` 则在 ``Session.send`` 真正发字节之前，位置最靠后也最可靠。
* 不改 ``story_agents`` 的提示词常量：那会连带改掉 ``story_fingerprint``
  （其中含 ``TIMELINE_AGENT_SYSTEM_PROMPT`` 的参与），让所有已缓存任务集体重跑。
  请求层兜底对指纹零影响。

## 开关

``CLOUD_STACK_JSON_MODE_GUARD=0`` 可关闭。默认开启 —— 关掉只会让上面那种配置
组合继续 400，没有任何收益。
"""

from __future__ import annotations

import os
from typing import Any

from . import config

_LOG_PREFIX = "[cloud_free_stack]"

# 与 patches._LOG_PREFIX 保持一致，但这里不能从 patches 导入：
# patches 反过来要 import 本模块（在 _install_llm_guard 里），会形成循环。

_GUARD_HINT = (
    "\n\n【输出格式硬约束】你必须只返回一个 JSON 对象（合法 JSON，不要 Markdown 代码块）。"
    "所有字段名与字符串值都用 JSON 语法书写。"
)

_HINT_MARK = "【输出格式硬约束】"


def _log(message: str) -> None:
    try:
        print(f"{_LOG_PREFIX} {message}", flush=True)
    except Exception:
        pass


def _debug(message: str) -> None:
    try:
        if config.debug():
            _log(message)
    except Exception:
        pass


def enabled() -> bool:
    """默认开启。

    读 ``os.environ`` 优先于面板层：与 ``agent1b_resilience._flag`` 同一条
    设计原则 —— 陈旧的面板配置不能悄悄压掉一个排障用的总开关。
    """
    raw = os.getenv("CLOUD_STACK_JSON_MODE_GUARD", "").strip().lower()
    if raw:
        return raw in {"1", "true", "yes", "on", "y"}
    try:
        return config.get_bool("CLOUD_STACK_JSON_MODE_GUARD", True)
    except Exception:
        return True


def _message_text(role: Any, content: Any) -> str:
    """把一条 message 的可见文本拼出来（content 可以是 str 或分段列表）。"""
    parts: list[str] = [str(role or "")]
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
    else:
        parts.append(str(content or ""))
    return " ".join(parts)


def messages_contain_json(messages: Any) -> bool:
    """``messages`` 全文（含 role）里有没有字面量 "json"。

    必须算上 role：OpenAI 官方校验把 role 名一起拼进去，所以一个名叫
    "json_formatter" 的工具角色也能通过校验。宁可多算，不可漏算 ——
    漏算会导致我们发出一个多余的提示词注入。
    """
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        if "json" in _message_text(message.get("role"), message.get("content")).lower():
            return True
    return False


def _inject_into_messages(messages: list[Any]) -> bool:
    """往最后一条 user 消息追加格式硬约束。就地修改，返回是否改动。"""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = content + _GUARD_HINT
            return True
        if isinstance(content, list):
            content.append({"type": "text", "text": _GUARD_HINT})
            return True
        message["content"] = str(content or "") + _GUARD_HINT
        return True
    return False


def repair_payload(payload: Any, *, base: str = "", url: Any = "") -> bool:
    """就地修补请求体，返回是否真的改动了它。

    四个前置条件缺一不可（见模块文档）。任何一条不成立都**不动**请求体。
    """
    if not isinstance(payload, dict):
        return False
    response_format = payload.get("response_format")
    if not isinstance(response_format, dict) or response_format.get("type") != "json_object":
        return False
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    if messages_contain_json(messages):
        return False
    return _inject_into_messages(messages)


def _looks_like_llm_call(url: Any, payload: Any, base: str) -> bool:
    if not base or not str(url).startswith(base):
        return False
    return isinstance(payload, dict) and isinstance(payload.get("messages"), list)


def install() -> bool:
    """挂到 ``requests.sessions.Session.request`` 上。幂等。"""
    try:
        import requests  # 延迟导入，避免拖慢模块加载
    except Exception:
        return False

    from requests.sessions import Session

    if getattr(Session.request, "_cloud_stack_json_guard", False):
        return True

    original_request = Session.request
    state = {"hits": 0}

    def _request_with_json_guard(self: Any, method: Any, url: Any, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        try:
            if enabled():
                try:
                    base = config.sensenova_base_url().rstrip("/")
                except Exception:
                    base = ""
                payload = kwargs.get("json")
                if _looks_like_llm_call(url, payload, base) and repair_payload(payload):
                    state["hits"] += 1
                    _debug(
                        "已为 response_format=json_object 的请求补上 JSON 输出约束"
                        f"（第 {state['hits']} 次，原提示词缺少字面量 json）"
                    )
        except Exception:  # noqa: BLE001 - 兜底补丁绝不能打断正常请求
            pass
        return original_request(self, method, url, *args, **kwargs)

    _request_with_json_guard._cloud_stack_json_guard = True  # type: ignore[attr-defined]
    _request_with_json_guard._cloud_stack_json_guard_hits = state  # type: ignore[attr-defined]
    Session.request = _request_with_json_guard  # type: ignore[assignment]
    _log("已安装 JSON 模式守卫：response_format=json_object 的请求将自动满足 OpenAI 契约")
    return True


def hit_count() -> int:
    """本进程内触发过多少次修补（自检与排障用）。"""
    try:
        from requests.sessions import Session

        state = getattr(Session.request, "_cloud_stack_json_guard_hits", None)
        return int(state["hits"]) if isinstance(state, dict) else 0
    except Exception:
        return 0


def installed() -> bool:
    try:
        from requests.sessions import Session

        return bool(getattr(Session.request, "_cloud_stack_json_guard", False))
    except Exception:
        return False
