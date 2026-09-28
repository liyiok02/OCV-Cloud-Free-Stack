"""把三个云端适配器挂载进 OCV。

每一处都只做「就地打补丁」，不重写任何 OCV 逻辑，因此：

* 原 IndexTTS / RunningHub / Gemini 代码路径全部保留，改配置即可回退；
* OCV 升级后只要函数签名不变，插件仍然可用；
* 关掉 ``CLOUD_STACK_ENABLED`` 后对 OCV 完全透明。

三个补丁点分别是：

1. ``backend.app.qwen_tts``   —— 把 ``synthesize_to_file`` 换成 MiMo 实现。
   module1 用 ``from backend.app.qwen_tts import synthesize_to_file`` 绑定了
   函数名，因此必须替换**模块属性**，而不是 patch ``module1_agent_director``
   （它是 ``__main__``，根本走不到导入钩子）。

2. ``backend.app.gemini_client`` —— 往 ``LANGUAGE_PROVIDER_OPTIONS`` 里插入
   商汤 provider。必须登记为 ``source: "official"``：
   ``_generate_openai_compatible_text`` 只对 official 源把 ``extra_body``
   平铺到请求根部，走 ``custom`` 会被包成 ``extra_body`` 子对象，
   导致 ``response_format: json_object`` **静默失效**。

3. 图片层不需要改代码 —— 把 ``IMAGE_API_BASE_URL`` 指向本地 shim 即可，
   由安装脚本写入 .env。

4. ``backend.app.main`` —— 把配置面板层叠加进 OCV 自己的配置视图
   （``_project_config_values``）。OCV 的凭据校验、提交门禁、
   前端 "已就绪" 状态灯全部读这个函数，而它只认 ``.env`` + ``os.environ``，
   看不见面板写进 ``var/config.json`` 的值。不叠这一层的话，用户在面板里
   填完 Key 仍然会被前端判定为"未配置"而拒绝提交。
"""

from __future__ import annotations

import importlib.machinery
import json
import os
import re
import sys
from typing import Any

from . import config

_PATCHED: set[str] = set()
_ATTEMPTS: dict[str, int] = {}
_MAX_ATTEMPTS = 3
_LOG_PREFIX = "[cloud_free_stack]"

# 只接受**固定采样参数**的模型 -> 必须使用的取值。
#
# 实测（2026-09-25，真实 Key）：`kimi-k3` 对参数有硬约束，传别的值直接 400：
#
#   temperature=0.3（OCV 默认） -> 400 field Temperature invalid, only 1 is allowed
#   top_p=1        （OCV 默认） -> 400 field TopP invalid, only 0.95 is allowed
#   temperature=1, top_p=0.95   -> 200 OK
#   presence/frequency_penalty  -> 不受限（任意值均可）
#
# 其余模型（deepseek-v4-flash / glm-5.2 / sensenova-6.8-flash-lite）发
# OCV 的全套默认参数都是 200，不受影响。
#
# ⚠ 铁律 34：同一份契约上的同类字段**一次修完**。这里一开始只修了
#   temperature，结果紧接着就撞上 top_p —— 两轮返工。凡"某模型只接受固定 X"
#   这类约束，要把该模型**全部**受限字段一起登记。
#
# 这是"参数契约"差异而非清单问题，所以在请求层纠正，模型清单仍由官方端点驱动。
PARAMETER_OVERRIDES: dict[str, dict[str, float]] = {
    "kimi-k3": {"temperature": 1.0, "top_p": 0.95},
}


def log(message: str) -> None:
    try:
        print(f"{_LOG_PREFIX} {message}", flush=True)
    except Exception:
        pass


def debug(message: str) -> None:
    if config.debug():
        log(message)


# --------------------------------------------------------------------------
# 1) TTS：MiMo 顶替 Qwen 槽位
# --------------------------------------------------------------------------

def _voice_supports_instructions(voice: str) -> bool:  # noqa: ARG001
    """MiMo 的预置音色始终可以和风格指令一起使用。"""
    return True


def _patch_qwen_tts(module: object) -> None:
    """TTS 补丁点。

    **1.10.0 起语义变了**：MiMo 不再无条件顶替 Qwen 槽位，而是作为**并列的第 4 个
    引擎**（前端下拉同时出现两个选项）。分流判定交给 ``mimo_engine``：

    * 独立引擎模式开（默认）→ **什么都不替换**。选 ``qwen`` 走 OCV 原生 DashScope；
      选 ``mimo`` 时任务会被归一化成 ``qwen`` 并打上 ``CLOUD_STACK_MIMO_ACTIVE=1``，
      由子进程内的 import 钩子把 ``synthesize_to_file`` 换成 MiMo
      （见 ``mimo_engine.apply_child_runtime``）。
    * 独立引擎模式关（``CLOUD_STACK_MIMO_SEPARATE_ENGINE=0``）→ 保留 1.9.x 的旧行为
      （全局顶替），供回退排查。

    这里**不再**无条件改 ``DEFAULT_VOICE``：那会让「Qwen 音色」的默认值变成 MiMo
    音色，正是「两个选项无法共存」的成因之一。
    """
    from . import mimo_engine

    if mimo_engine.enabled():
        # 并列模式：仅当本进程被标记为 MiMo 任务时才替换。
        # 后端进程通常不带这个标记；真正生效的是 module1 子进程。
        if mimo_engine.apply_child_runtime():
            log("MiMo 独立引擎：本进程合成分流到 MiMo（qwen 通道未被占用）")
        return

    # ---- 以下为旧行为（顶替），仅在独立引擎模式关闭时走 ----
    # 延迟导入：chains 会拉起 requests，不想在每个子进程启动时都付这个成本
    from . import mimo_tts

    module.synthesize_to_file = mimo_tts.synthesize_to_file  # type: ignore[attr-defined]
    module.voice_supports_instructions = _voice_supports_instructions  # type: ignore[attr-defined]
    # main.py 用 DEFAULT_VOICE 做 Pydantic 字段默认值，一并换成 MiMo 音色，
    # 这样即使前端没传音色也不会落到一个 Qwen 专有名字上。
    module.DEFAULT_VOICE = config.mimo_default_voice()  # type: ignore[attr-defined]
    log(
        f"已接管 TTS（旧模式：顶替 qwen 槽位）：synthesize_to_file -> MiMo"
        f"（{config.mimo_model()}，默认音色 {config.mimo_default_voice()}）"
    )


# --------------------------------------------------------------------------
# 2) LLM：新增商汤 SenseNova provider
# --------------------------------------------------------------------------

def _sensenova_entry() -> dict[str, object]:
    base = config.sensenova_base_url()
    model = config.sensenova_model()
    return {
        # 名字里的 sensenova 是历史遗留的 provider id（OCV 侧选择器用），
        # 实际接入的是「任意 OpenAI 兼容服务商」：地址 / Key / 模型全在面板配。
        "label": "OpenAI 兼容接口（云免费栈）",
        "family": "sensenova",
        "family_label": "OpenAI 兼容（云免费栈）",
        # 关键：official 才会让 response_format / reasoning_effort 平铺到请求根部
        "source": "official",
        "key_env": "SENSENOVA_API_KEY",
        "base_env": "SENSENOVA_API_BASE",
        "model_env": "SENSENOVA_MODEL",
        # language_base_url() 对 official 源强制使用 default_base 并忽略 base_env，
        # 因此这里把用户配置的地址直接固化成 default_base（防串厂商，又支持自建）。
        "default_base": base,
        "default_model": model,
        "protocol": "openai",
        "models": [
            {"value": "deepseek-v4-pro", "label": "DeepSeek V4 Pro（商汤代理·推荐）"},
            {"value": "deepseek-v4-flash", "label": "DeepSeek V4 Flash（省额度）"},
            {"value": "sensenova-6.8-flash-lite", "label": "SenseNova 6.8 Flash-Lite（多模态）"},
        ],
        "allow_custom_model": True,
    }


def _jethub_entry() -> dict[str, object]:
    """云端 OAuth（本机桥）作为**第二个语言模型 provider**。

    ## 为什么它是独立 provider 而不是复用 sensenova

    用户明确要求：**语言模型与云端 OAuth 都是 LLM，不能同时使用**，
    要在语言模型分页里下拉选择「自行用 API」还是「云端 OAuth」。

    所以这里注册一个独立的 `jethub` provider，与 `sensenova` **并列**；
    面板的 `LLM_SOURCE` 字段决定 `LANGUAGE_PROVIDER` 最终取哪个 —— 二者互斥。

    ## 与 sensenova 的关键差异

    | 维度 | sensenova | jethub |
    |---|---|---|
    | 地址 | 用户填的服务商 | 本机桥 `http://127.0.0.1:<port>/v1` |
    | 凭据 | 用户填的 API Key | **桥自己管账号池**（OCV 不需要 Key） |
    | 模型 | 面板在线拉取 | 桥从已登录账号拉取 |

    因为它指向**本机**，`source` 用 `custom` 而不是 `official`：
    `official` 会让 `language_base_url()` 强制走 `default_base` 并忽略
    `base_env`，而桥的端口是可配置的（`JETHUB_BRIDGE_PORT`），
    必须允许面板值实时覆盖 —— `custom` 恰好读 `base_env`。
    """
    base = f"http://127.0.0.1:{config.jethub_bridge_port()}/v1"
    model = config.jethub_model() or "deepseek-v4-flash"
    return {
        "label": "云端 OAuth（免费额度）",
        "family": "jethub",
        "family_label": "云端 OAuth",
        # custom：允许 base_env 覆盖 default_base（桥端口可配）
        "source": "custom",
        "key_env": "JETHUB_DUMMY_KEY",
        "base_env": "JETHUB_BRIDGE_BASE_URL",
        "model_env": "JETHUB_MODEL",
        "default_base": base,
        "default_model": model,
        "protocol": "openai",
        # 模型清单由桥提供（面板的 llm_jethub 用途），这里只放离线回退
        "models": [
            {"value": "deepseek-v4-flash", "label": "DeepSeek V4 Flash（CodeBuddy·快）"},
            {"value": "glm-5.2", "label": "GLM-5.2（CodeBuddy）"},
            {"value": "kimi-k2.7", "label": "Kimi K2.7（CodeBuddy）"},
        ],
        "allow_custom_model": True,
        # 桥不需要 Key —— 置 True 让 OCV 的「已配置」校验只看 base+model
        "optional_key": True,
    }


def _patch_gemini_client(module: object) -> None:
    # 重试策略必须装在**任何提前 return 之前**（F-010 的教训：早退会把补丁一起掐掉）。
    # 它与下面的 provider 注册是两件独立的事：即使 provider 已注册过（重复调用 /
    # 其它补丁先跑），重试策略也必须确保装上。
    _install_llm_retry(module)

    # 基址实时化同理：必须早于 provider 注册的早退（F-015）。
    _install_live_base_url()

    options = getattr(module, "LANGUAGE_PROVIDER_OPTIONS", None)
    if not isinstance(options, dict):
        return
    registered: list[str] = []
    if "sensenova" not in options:
        options["sensenova"] = _sensenova_entry()
        registered.append("sensenova")
    # 云端 OAuth 作为**并列的第二个 provider**（两者互斥，由面板 LLM_SOURCE 决定用哪个）。
    # 总开关关掉时不注册 —— 那样 OCV 的选择器里根本看不到它。
    if config.jethub_enabled() and "jethub" not in options:
        options["jethub"] = _jethub_entry()
        registered.append("jethub")
    if registered:
        log(f"已注册语言模型 provider：{', '.join(registered)}")

    original = getattr(module, "_generate_openai_compatible_text", None)
    if callable(original):

        def _generate_with_floor(*args, **kwargs):  # type: ignore[no-untyped-def]
            """按配置调整输出预算（F-017）。

            两种语义：

            * ``SENSENOVA_MAX_TOKENS`` 为**正整数** ⇒ "抬下限"：低于该值就抬上来
              （给推理模型的思考 token 留预算；保留给需要精确控制的场景）。
            * 为 **0（默认）** ⇒ **交给模型自己的默认值**：把 ``max_output_tokens``
              置成 ``None``，等会儿在请求体层把 ``max_tokens`` 键**删掉**。

            为什么必须删而不能只传 None：OCV 源码是
            ``max_output_tokens or int(os.getenv("GEMINI_MAX_TOKENS", "4096"))``，
            传 None 会**落到 4096** —— 比原来的 16384 更糟。真正的"用模型默认"
            只能靠发请求时**不带这个键**。
            """
            try:
                effective = kwargs.get("provider") or module._provider()  # type: ignore[attr-defined]
            except Exception:
                effective = kwargs.get("provider")
            if str(effective or "").strip().lower() == "sensenova":
                floor = config.sensenova_max_tokens()
                if floor <= 0:
                    # 0 = 不限制：置 None，交给 body 注入器删键
                    kwargs["max_output_tokens"] = None
                else:
                    current = kwargs.get("max_output_tokens")
                    try:
                        current_value = int(current or 0)
                    except (TypeError, ValueError):
                        current_value = 0
                    if current_value < floor:
                        kwargs["max_output_tokens"] = floor
            try:
                return original(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 -- 转译后原样抛回
                hint = _truncation_hint(exc) or _plan_restricted_hint(exc)
                if hint:
                    raise type(exc)(f"{exc}{hint}") from exc
                raise

        module._generate_openai_compatible_text = _generate_with_floor  # type: ignore[attr-defined]

    _install_llm_body_injector()
    # json_object 契约兜底：与扩展参数注入器是两件事，各自独立安装。
    # 顺序无所谓（一个改 response_format 之外的键，一个只在缺 json 时补提示词）。
    _install_json_mode_guard()


def _parameter_overrides_for(model: Any) -> dict[str, float]:
    """该模型需要强制的采样参数（空 dict 表示不干预）。"""
    name = str(model or "").strip()
    if not name:
        return {}
    return PARAMETER_OVERRIDES.get(name, {})


def _max_tokens_unlimited() -> bool:
    """是否"不限制输出"（即应从请求体里删掉 ``max_tokens``）。

    对应 ``SENSENOVA_MAX_TOKENS=0``（**默认**）。取不到配置时按"不限制"处理 ——
    让模型用自己的默认值，比我们猜一个小数字安全（见 F-017）。
    """
    try:
        return config.sensenova_max_tokens() <= 0
    except Exception:  # noqa: BLE001
        return True


def _plan_restricted_hint(exc: BaseException) -> str:
    """模型被套餐挡住时，给一条可操作的提示（F-015）。

    用户实测：``HTTP 403 model is not available in the current token plan``。
    这条报错本身已经很清楚，但用户不知道该**改成哪个**模型 —— 而面板下拉里
    还列着那个不可用的型号（``/models`` 会返回它）。所以这里明确指出
    "清单会剔除它 + 推荐换成哪个"。

    只对识别得出的报错追加，其余原样抛出（不干扰真实故障的排查）。
    """
    text = str(exc)
    if "token plan" not in text.lower():
        return ""
    from . import models_catalog

    restricted = "、".join(sorted(models_catalog._PLAN_RESTRICTED))
    if not restricted:
        return ""
    return (
        f"｜诊断：模型「{restricted}」不在你当前的 token plan 内（服务商 /models 会列出它，"
        "但实际调用返回 403），**不是 Key 或余额问题**。"
        "对策：面板「语言模型」分页把模型改选为 deepseek-v4-pro 或 deepseek-v4-flash"
        "（推荐项带 ★）。该型号已从下拉清单中剔除，以免再次误选。"
    )


def _install_llm_retry(module: object) -> None:
    """把语言模型的 30 档阶梯重试接到 OCV 自己的重试循环上。

    详见 ``llm_retry`` 模块文档。要点：**只替换策略函数，不新增重试循环**，
    否则会与 OCV 内层循环相乘（3 × 31 = 93 次请求）——典型的"重试放大"事故。

    安装失败只记日志：重试策略是增强，不是 OCV 能否运行的前提。
    """
    from . import llm_retry

    try:
        if llm_retry.install(module):
            return
        log("语言模型阶梯重试未装上（未找到 _gemini_retry_count / _gemini_retry_delay）")
    except Exception as exc:  # noqa: BLE001 - 增强能力失败不能拖垮 OCV
        log(f"语言模型阶梯重试安装失败，将保持 OCV 原生重试：{type(exc).__name__}: {exc}")


def _install_live_base_url() -> None:
    """让 provider 的**基址与模型实时跟随面板**，而不是被打补丁那一刻冻结（F-015）。

    ## 为什么必须打这个补丁

    OCV 的 ``language_base_url()`` 对 ``source == "official"`` 的 provider
    **强制使用 ``config["default_base"]`` 并忽略 ``base_env``**（源码注释写明
    这是"防止官方凭据串到别的厂商"）：

    ```python
    if config.get("source") == "official":
        return str(config["default_base"]).rstrip("/")
    return os.getenv(config["base_env"], config["default_base"]).strip().rstrip("/")
    ```

    而本插件为了"既防串厂商、又支持用户自建"，把面板地址在 ``_sensenova_entry()``
    里**固化**进了 ``default_base`` —— 那个 dict 只在补丁安装时构造一次。

    于是产生一个**不对称**的致命组合：

    | 字段 | 取值时机 | 改面板后 |
    |---|---|---|
    | ``default_base`` | 打补丁那一刻 | **不跟进**（旧地址） |
    | ``language_model()`` | 每次调用读 ``os.environ`` | 跟进（新模型名） |

    ⇒ **新的模型名被发到旧的服务地址**。用户实测到的正是这个：面板已指向商汤
    ``token.sensenova.cn``、模型是 ``deepseek-v4.1-flash``，请求却仍打在
    ``api.deepseek.com``，于是 DeepSeek 回
    ``401 Authentication Fails, Your api key: ****QClD is invalid``
    —— 一条**看起来像 Key 失效、实则地址串了**的报错，极具误导性。

    ## 修法

    包装 ``language_base_url``：本插件的 provider 一律**实时**读面板。
    同时刷新 ``LANGUAGE_PROVIDER_OPTIONS["sensenova"]`` 的 ``default_base`` /
    ``default_model``，让其它读这张表的路径也保持一致。

    ## 铁律 29：必须连 from-import 的调用方一起换

    ``backend/app/main.py`` 用 ``from .gemini_client import language_base_url``
    绑定了名字（preflight 的 ``/models`` 探测就读它），所以 ``main`` 命名空间里
    那一份也要一起换掉，否则「面板测试连通性」仍会打旧地址。
    """
    import sys

    module = sys.modules.get("backend.app.gemini_client")
    if module is None:
        return

    original = getattr(module, "language_base_url", None)
    if not callable(original):
        return
    if getattr(original, "_cloud_stack_live_base", False):
        # 已装过：仍要把 main 里那份对齐（main 可能后导入）
        _sync_main_base_url(module)
        return

    def _base_url_live(provider: Any = None) -> str:
        """本插件的 provider 实时读面板；其余 provider 原样透传。"""
        try:
            name = str(provider or "").strip().lower()
            if not name:
                name = str(module._provider() or "").strip().lower()  # type: ignore[attr-defined]
            if name == "sensenova":
                live = config.sensenova_base_url()
                if live:
                    # 顺带把 provider 表的 default_base/default_model 对齐。
                    # 有些路径（面板状态、language_provider_status）直接读那张表，
                    # 只在安装时刷一次的话，运行中改面板它们又会落后。
                    refresh_provider_entry(module)
                    return live
        except Exception:  # noqa: BLE001 - 取不到就回落到原生
            pass
        return original(provider)

    _base_url_live._cloud_stack_live_base = True  # type: ignore[attr-defined]
    _base_url_live._cloud_stack_live_base_original = original  # type: ignore[attr-defined]
    module.language_base_url = _base_url_live  # type: ignore[attr-defined]

    # 模型名同理：原生 `language_model()` 读的是 ``os.environ``，那是**进程启动时
    # 的快照**（只有面板保存走 export_store_to_environ 才会更新）。
    # 基址与模型必须是**同一份实时快照**，否则又会退化成"一个新一个旧"——
    # 只是这次的触发条件变成"没走面板保存路径改的配置"。
    _install_live_model(module)

    # provider 表里的 default_base / default_model 也刷成当前值：
    # 有些路径（面板状态、language_provider_status）直接读这张表。
    refresh_provider_entry(module)

    _sync_main_base_url(module)
    log("已接管语言模型基址解析：面板改服务地址后立即生效（不再冻结在启动值）")


def _install_live_model(module: Any) -> None:
    """让 ``language_model()`` 对本插件 provider 实时读面板（与基址同源）。

    原生实现读 ``os.environ``（进程启动快照，只在面板保存时才被
    ``export_store_to_environ`` 刷新）。基址已经实时了，模型名却还是快照，
    两者就会在某些路径下**不同步** —— 那正是 F-015 的病灶形态。
    这里把它们统一到"都读面板层"。

    同样要连 ``main`` 命名空间一起换（铁律 29）。
    """
    import sys

    original = getattr(module, "language_model", None)
    if not callable(original) or getattr(original, "_cloud_stack_live_model", False):
        _sync_main_model(module)
        return

    def _model_live(provider: Any = None) -> str:
        try:
            name = str(provider or "").strip().lower()
            if not name:
                name = str(module._provider() or "").strip().lower()  # type: ignore[attr-defined]
            if name == "sensenova":
                live = config.sensenova_model()
                if live:
                    return live
        except Exception:  # noqa: BLE001
            pass
        return original(provider)

    _model_live._cloud_stack_live_model = True  # type: ignore[attr-defined]
    _model_live._cloud_stack_live_model_original = original  # type: ignore[attr-defined]
    module.language_model = _model_live  # type: ignore[attr-defined]
    _sync_main_model(module)


def _sync_main_model(module: Any) -> None:
    """把 ``main`` 命名空间里冻结的 ``language_model`` 换成当前这份（铁律 29）。"""
    import sys

    main = sys.modules.get("backend.app.main")
    if main is None:
        return
    live = getattr(module, "language_model", None)
    if callable(live) and getattr(main, "language_model", None) is not live:
        main.language_model = live  # type: ignore[attr-defined]


def refresh_provider_entry(module: Any) -> None:
    """把 provider 表里的 ``default_base`` / ``default_model`` 刷成当前面板值。"""
    try:
        options = getattr(module, "LANGUAGE_PROVIDER_OPTIONS", None)
        if not isinstance(options, dict):
            return
        entry = options.get("sensenova")
        if isinstance(entry, dict):
            entry["default_base"] = config.sensenova_base_url()
            entry["default_model"] = config.sensenova_model()
        # 云端 OAuth 的基址随端口走、模型随面板走，同样要实时刷新
        jethub = options.get("jethub")
        if isinstance(jethub, dict):
            jethub["default_base"] = config.jethub_bridge_base_url()
            model = config.jethub_model()
            if model:
                # 模型值形如 `provider::model`；OCV 只把它当字符串发给桥，
                # 桥自己按 `::` 拆开路由（见 jethub/bridge.mjs parseModelRef）。
                jethub["default_model"] = model
    except Exception:  # noqa: BLE001
        pass


def _sync_language_provider(module: Any) -> None:
    """把 «语言模型来源» 这个**互斥开关**落到 OCV 的 `LANGUAGE_PROVIDER`。

    用户明确要求：「语言模型和云端 OAuth 两项都是 LLM，不能同时使用，
    在语言模型中给用户下拉选择，是自行用 API 还是云端 OAuth」。

    ## 为什么必须在这一层做

    OCV 用 `LANGUAGE_PROVIDER`（环境变量）决定走哪个 provider。面板保存时
    会把它物化进 `os.environ`，但**已导入的模块**里可能已有缓存视图、
    且 `.env` 里那份是安装时的基线。所以这里每次请求都从
    `config.resolved_language_provider()` 重新推一遍，保证：
      * 面板一改，下一次请求就换路（无需重启）；
      * 两路永远只可能有一个生效 —— 不会出现「两个都填了、走哪个看运气」。

    ## 与 `_CONFIG_VIEW_MAP` 的关系

    那个映射表负责把**面板层**的值叠加给 OCV 的凭据校验；
    这里负责的是**语义层**：把 `LLM_SOURCE` 这个插件自有开关
    翻译成 OCV 认识的 `LANGUAGE_PROVIDER`。
    """
    try:
        import os

        target = config.resolved_language_provider()
        if os.environ.get("LANGUAGE_PROVIDER") != target:
            os.environ["LANGUAGE_PROVIDER"] = target
            debug(f"语言模型来源已切换：LANGUAGE_PROVIDER={target}")
        # 云端 OAuth 走本机桥，基址必须跟着端口实时变
        if target == "jethub":
            base = config.jethub_bridge_base_url()
            if os.environ.get("JETHUB_BRIDGE_BASE_URL") != base:
                os.environ["JETHUB_BRIDGE_BASE_URL"] = base
    except Exception as exc:  # noqa: BLE001
        debug(f"同步语言模型来源失败：{type(exc).__name__}: {exc}")


def _sync_main_base_url(module: Any) -> None:
    """把 ``main`` 命名空间里 from-import 冻结的 ``language_base_url`` 一并换掉。

    铁律 29：``from X import f`` 之后只换 ``X.f`` 是**没用的**，调用方仍攥着旧函数。
    本函数做的是"跟随"—— 只认 gemini_client 上当前那份，保证两处永远同一个函数。
    """
    import sys

    main = sys.modules.get("backend.app.main")
    if main is None:
        return
    live = getattr(module, "language_base_url", None)
    if callable(live) and getattr(main, "language_base_url", None) is not live:
        main.language_base_url = live  # type: ignore[attr-defined]


def _sync_base_url_from_gemini_client(main_module: Any) -> None:
    """从 gemini_client 拉取"当前那份" ``language_base_url`` 给 ``main``。

    与 :func:`_sync_main_base_url` 方向相反：那个是 gemini_client 打完补丁后
    主动推给 main；这个是 main 被导入后自己来拉。两条路都要有 ——
    谁先谁后取决于 OCV 的导入顺序，只做一边必然漏掉一种时序。
    """
    import sys

    gc = sys.modules.get("backend.app.gemini_client")
    if gc is None:
        return
    live = getattr(gc, "language_base_url", None)
    if callable(live) and getattr(main_module, "language_base_url", None) is not live:
        main_module.language_base_url = live  # type: ignore[attr-defined]
    live_model = getattr(gc, "language_model", None)
    if callable(live_model) and getattr(main_module, "language_model", None) is not live_model:
        main_module.language_model = live_model  # type: ignore[attr-defined]
    refresh_provider_entry(gc)


def _install_json_mode_guard() -> None:
    """保证 ``response_format: json_object`` 的请求满足 OpenAI 契约。

    详见 ``json_mode_guard`` 模块文档。这里只负责调用与日志，不重复实现。
    """
    from . import json_mode_guard

    try:
        json_mode_guard.install()
    except Exception as exc:  # noqa: BLE001 - 兜底补丁失败不能拖垮 OCV
        log(f"JSON 模式守卫安装失败，将保持 OCV 原生行为：{type(exc).__name__}: {exc}")


_REASONING_RE = re.compile(r"reasoning_tokens['\"]?:\s*(\d+)")
_COMPLETION_RE = re.compile(r"completion_tokens['\"]?:\s*(\d+)")


def _truncation_hint(exc: BaseException) -> str:
    """输出被长度截断、且思考 token 占满了预算时，给一条可操作的提示。"""
    if type(exc).__name__ != "GeminiOutputTruncated":
        return ""
    text = str(exc)
    reasoning_match = _REASONING_RE.search(text)
    if not reasoning_match:
        return ""
    # completion_tokens 取「不含 _details 里 reasoning 的那个」：
    # 真实报错里 'completion_tokens' 出现在 'completion_tokens_details' 之前，
    # 取最后一个独立键的值不可靠，直接取所有匹配里的最大值即可——
    # reasoning_tokens 总是 <= completion_tokens。
    completion = max(int(v) for v in _COMPLETION_RE.findall(text))
    reasoning = int(reasoning_match.group(1))
    if completion <= 0 or reasoning * 10 < completion * 9:
        return ""
    return (
        "｜诊断：思考 token 占了输出的九成以上，正文被挤没了。"
        "对策：面板「语言模型」分页把「扩展请求参数」填为"
        ' {"reasoning_effort": "none"} 关闭思考（需重启 OCV 生效），'
        "或直接换非思考型模型。"
    )


def _llm_extra_body() -> dict[str, Any]:
    """语言模型请求的额外参数：思考开关 + 面板扩展参数。

    **字段级合并，不是整体覆盖。** 这一点很关键：

    * 「深度思考」= off 时占用 ``reasoning_effort`` / ``thinking`` 两个键；
      用户把它切到 low/medium/high 后，这两个键**必须**改由档次决定。
    * 但面板上 ``CLOUD_STACK_LLM_EXTRA_BODY`` 的**历史遗留值**恰好是
      ``{"reasoning_effort": "none"}``（旧版本靠它关思考）。如果简单
      ``dict.update()``，这个陈旧值会把用户刚开启的档位又按回 ``none``
      —— 用户看到「已开启深度思考」但实际请求仍是关闭，属于**静默失效**。

    因此：扩展参数里凡是与思考相关的键（``reasoning_effort`` /
    ``thinking``），只在思考档位为 off 时才采纳；用户显式开启思考后，
    以档位为准。其它键（服务商特有参数）照常透传。
    """
    thinking = _thinking_extra_body()
    merged: dict[str, Any] = dict(thinking)
    thinking_enabled = llm_thinking_mode() != "off"

    raw = str(config.get("CLOUD_STACK_LLM_EXTRA_BODY") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                if thinking_enabled and key in {"reasoning_effort", "thinking"}:
                    # 用户已在面板上显式开启深度思考 —— 不让陈旧的历史
                    # 配置把它按回去。仅记录一条调试日志，便于排查。
                    debug(
                        f"扩展参数里的 {key}={value!r} 已被「深度思考」档位"
                        f"（{llm_thinking_mode()}）忽略"
                    )
                    continue
                merged[key] = value
    return merged


# 「深度思考」档位 -> reasoning_effort 取值。off 会额外走 ``thinking`` 开关。
_THINKING_LEVELS = {
    "off": None,
    "none": None,
    "low": "low",
    "medium": "medium",
    "high": "high",
}
_DEFAULT_THINKING = "off"


def llm_thinking_mode() -> str:
    """面板上的「深度思考」档位。默认 ``off``（关闭）。"""
    raw = str(config.get("CLOUD_STACK_LLM_THINKING") or "").strip().lower()
    if raw in _THINKING_LEVELS:
        return raw
    if raw in {"disabled", "disable", "false", "no", "0"}:
        return "off"
    if raw in {"enabled", "enable", "true", "yes", "on", "auto"}:
        return "medium"
    return _DEFAULT_THINKING


def _thinking_extra_body() -> dict[str, Any]:
    """把「深度思考」档位翻译成上游认识的请求字段。

    设计取舍（**默认关闭**）：

    * 关闭时发 ``reasoning_effort="none"`` **并且**发
      ``thinking={"type":"disabled"}`` —— 两个键都发是刻意的。实测
      DeepSeek 官方接口认 ``reasoning_effort``；而 OpenAI 兼容的第三方
      网关更常实现 ``thinking`` 开关。同时发出可以让两种实现都被覆盖，
      未知键会被服务端忽略，代价仅为几十字节。
    * 开启时**只**发 ``reasoning_effort=<档位>``，**不发** ``thinking``：
      避免 ``thinking=enabled`` 把某些网关的默认档位顶掉，
      让 ``reasoning_effort`` 这个语义更细的键说了算。
    * 档位为 off 时不发 ``reasoning_effort`` 的自定义值 ——
      OCV 自己的 ``GEMINI_REASONING_EFFORT`` 恰好会把 ``none`` 过滤掉不发，
      所以这里必须由插件补上，见 ``_install_llm_body_injector``。

    这样「默认关闭」在任何 OpenAI 兼容服务商上都能落地，而用户想开启时
    不会因为厂商差异而失效。
    """
    mode = llm_thinking_mode()
    effort = _THINKING_LEVELS.get(mode)
    if effort is None:
        return {"thinking": {"type": "disabled"}, "reasoning_effort": "none"}
    return {"reasoning_effort": effort}


def _install_llm_body_injector() -> None:
    """把面板扩展参数注入语言模型的 ``/chat/completions`` 请求体根部。

    OCV 的 ``_generate_openai_compatible_text`` 只认
    ``GEMINI_REASONING_EFFORT``（且把 ``none`` 过滤掉）和
    ``response_format``，没有任意参数的口子；改 OCV 源码违背零改动原则，
    所以在 ``requests.post`` 这一层做**范围严格受限**的合并：

    * 仅当 URL 以面板配置的语言模型地址开头（默认同 base_url）；
    * 且请求体是带 ``messages`` 的 dict（chat completions 形态）——
      图片请求没有 ``messages``、TTS 走本地 shim，天然排除在外。
    """
    import requests  # 延迟导入，避免拖慢模块加载

    if getattr(requests.post, "_cloud_stack_body_injector", False):
        return

    original_post = requests.post

    def _post_with_extra_body(url: Any, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        extra = _llm_extra_body()
        # ## ⚠ 必须认**当前启用来源**的 base（F-025 的真实缺陷）
        #
        # 首版只取 `config.sensenova_base_url()`。用户在语言模型页选了
        # 「云端 OAuth」后，请求打向**本机桥**（`127.0.0.1:<port>/v1`），
        # 于是 url 不以商汤地址开头 → 这个注入器**整段静默失效**：
        #   * `max_tokens` 的「留 0 不指定」不再生效（退回 OCV 的 4096）；
        #   * `reasoning_effort`（深度思考档位）不生效；
        #   * `CLOUD_STACK_LLM_EXTRA_BODY` 自定义字段不生效。
        # 没有任何报错 —— 只是「设置看起来不起作用」，极难定位。
        #
        # 所以候选 base 要**同时包含两路**，按当前来源优先。
        candidates: list[str] = []
        try:
            primary = (
                config.jethub_bridge_base_url()
                if config.llm_source() == "jethub"
                else config.sensenova_base_url()
            )
            candidates.append(primary.rstrip("/"))
        except Exception:  # noqa: BLE001
            pass
        # 另一路也放进来：切换来源后**正在飞行中**的请求不该被漏掉
        # （面板改配置 → 环境变量立刻变，但旧进程可能还持着旧 base）
        try:
            candidates.append(config.sensenova_base_url().rstrip("/"))
        except Exception:  # noqa: BLE001
            pass
        try:
            candidates.append(config.jethub_bridge_base_url().rstrip("/"))
        except Exception:  # noqa: BLE001
            pass
        candidates = [c for c in dict.fromkeys(candidates) if c]

        payload = kwargs.get("json")
        matched = any(str(url).startswith(c) for c in candidates)
        if (
            matched
            and isinstance(payload, dict)
            and isinstance(payload.get("messages"), list)
        ):
            merged = dict(payload)
            if extra:
                merged.update(extra)
            # 参数契约纠正也在这里做：这是**唯一**能同时看到
            # ``model`` 与 ``temperature`` / ``top_p`` 的地方 ——
            # 它们在 ``_generate_openai_compatible_text`` 里是本地变量
            # （top_p 甚至来自 os.getenv），包装那个函数够不着（F-015 第三处）。
            fixed = _parameter_overrides_for(merged.get("model"))
            if fixed:
                changed: list[str] = []
                for key, value in fixed.items():
                    current = merged.get(key)
                    try:
                        same = current is not None and abs(float(current) - float(value)) < 1e-9
                    except (TypeError, ValueError):
                        same = False
                    if not same:
                        changed.append(f"{key}={current!r}→{value}")
                        merged[key] = value
                if changed:
                    debug(
                        f"{merged.get('model')} 只接受固定采样参数，已纠正："
                        f"{'、'.join(changed)}（否则上游返回 400）"
                    )
            # 输出预算：SENSENOVA_MAX_TOKENS=0（默认）时**删掉 max_tokens 键**，
            # 交给模型自己的默认值 —— 这是"按模型自动"的唯一可靠做法（F-017）。
            #
            # 必须删而不能留成 None：OCV 源码把 None 当"没给"，
            # 于是回落到 ``GEMINI_MAX_TOKENS`` 默认 **4096**，比原来的 16384 更糟。
            # 上游只认"键不存在"才是它自己的默认（实测：省略时给 65536）。
            if _max_tokens_unlimited():
                if "max_tokens" in merged:
                    merged.pop("max_tokens", None)
                    debug("已按配置移除 max_tokens，交由模型自身默认值决定输出上限（F-017）")
            else:
                # ## 非零值 = **下限**语义（F-026：此前只写了文档，没写代码）
                #
                # 面板的帮助文字承诺：「填正整数则作为**下限**生效
                # （低于它会被抬到该值）」。但首版只实现了「0 就删键」，
                # 非零时**什么都不做** —— 于是 OCV 传下来的 4096 原样发出，
                # 用户填的 32768 被静默忽略。
                #
                # 这条断言（self_test 1t-5）就是为此存在的，但因为它跑在
                # 「来源 = custom」的默认路径上，长期没被发现；
                # 直到 F-025 修好 base 匹配、注入器真的开始工作才暴露。
                limit = config.sensenova_max_tokens()
                try:
                    current = merged.get("max_tokens")
                    current_num = int(current) if current is not None else 0
                except (TypeError, ValueError):
                    current_num = 0
                if limit > current_num:
                    merged["max_tokens"] = limit
                    debug(
                        f"输出上限按面板设置抬到 {limit}"
                        f"（模型默认/OCV 传入值 {current_num or '未指定'} 更低）"
                    )
            if merged != payload:
                kwargs["json"] = merged
        return original_post(url, *args, **kwargs)

    _post_with_extra_body._cloud_stack_body_injector = True  # type: ignore[attr-defined]
    requests.post = _post_with_extra_body  # type: ignore[assignment]
    log("已安装语言模型扩展参数注入器（CLOUD_STACK_LLM_EXTRA_BODY）")


# --------------------------------------------------------------------------
# 3) 配置视图：让 OCV 的凭据校验看得见面板层
# --------------------------------------------------------------------------

# OCV 校验时看到的环境键 -> 插件配置键
_CONFIG_VIEW_MAP: dict[str, str] = {
    # qwen 槽位已被 MiMo 接管，这个键的凭据语义也跟着换成 MiMo 的 Key。
    # 不换的话，面板里填了 MiMo Key，前端仍然显示"Qwen 未配置"并禁止提交。
    "DASHSCOPE_API_KEY": "MIMO_API_KEY",
    "SENSENOVA_API_KEY": "SENSENOVA_API_KEY",
    "SENSENOVA_API_BASE": "SENSENOVA_API_BASE",
    "SENSENOVA_MODEL": "SENSENOVA_MODEL",
    "IMAGE_API_BASE_URL": "IMAGE_API_BASE_URL",
    "IMAGE_MODEL_ID": "IMAGE_MODEL_ID",
    # ⚠ `LANGUAGE_PROVIDER` **刻意不在这里**。
    #
    # 它由互斥开关 `LLM_SOURCE` 推导（见 `_sync_language_provider`），
    # 而不是「面板层有值就覆盖」—— 后者会让 `LANGUAGE_PROVIDER` 成为
    # 第三个真相源，出现「界面选了云端 OAuth、这个键还停在 sensenova」的错配。
    # 云端 OAuth 的模型值同理，由 `JETHUB_MODEL` 直接供 `model_env` 读取。
}


def _patch_main(module: object) -> None:
    # 自愈必须放在任何提前 return **之前**：它只依赖 frontend/index.html 与
    # .pth，和下面的配置视图补丁没有任何关系。把顺序倒过来的话，一旦将来
    # 某次更新改掉了 _project_config_values 的名字，这里会提前返回，
    # 连带把「更新后自动重建按钮」这条唯一的自动路径一起掐掉 ——
    # 那正是本模块要防的场景，不能让它自己有同样的脆弱性。
    _schedule_derived_state_reapply()

    # 语言模型来源（互斥开关）也必须早于任何提前 return —— 它只依赖插件自己的
    # 配置，与下面的 provider 注册无关。放在早退之后的话，一旦
    # `_project_config_values` 改名，这个开关就会静默失效（F-010 的同款陷阱）。
    _sync_language_provider(module)

    # 铁律 29：main 用 `from .gemini_client import language_base_url` 冻结了名字。
    # 这一步同样必须早于下面的提前 return —— main 可能是在 gemini_client
    # 之后才导入的，那时 _install_live_base_url 里的同步还没发生；
    # 放在早退之后的话，重复打补丁的路径就再也补不上（F-010 的同款陷阱）。
    _sync_base_url_from_gemini_client(module)

    # MiMo 独立引擎的安装点**必须在这里**：它要改 `GenerateRequest.tts_engine`
    # 的 Literal 并重建**路由**的 TypeAdapter，而路由是在 `backend.app.main`
    # 模块体执行时注册的 —— 只有此刻（模块体已跑完）才能看见 `app.routes`。
    # 同样必须早于下面的提前 return（否则 _project_config_values 一变名就没了）。
    try:
        from . import mimo_engine

        if mimo_engine.enabled():
            mimo_engine.install()
    except Exception as exc:  # noqa: BLE001 - 独立引擎装不上不能拖垮 OCV
        debug(f"MiMo 独立引擎安装未完成：{type(exc).__name__}: {exc}")

    original = getattr(module, "_project_config_values", None)
    if not callable(original) or getattr(original, "_cloud_stack_wrapped", False):
        return

    def _with_panel_layer(*args: Any, **kwargs: Any) -> Any:
        values = original(*args, **kwargs)
        if not isinstance(values, dict):
            return values
        merged = dict(values)
        for target_key, source_key in _CONFIG_VIEW_MAP.items():
            value = config.get(source_key)
            if value:
                merged[target_key] = value
        return merged

    _with_panel_layer._cloud_stack_wrapped = True  # type: ignore[attr-defined]
    module._project_config_values = _with_panel_layer  # type: ignore[attr-defined]

    # main.py 在导入时就 `from .qwen_tts import ... voice_supports_instructions`
    # 绑定了名字，必须连它一起换掉，否则"配音描述 + 国际音色"仍会被前端拦。
    #
    # **1.10.0 起只在旧模式（顶替）下才改**：并列模式下 qwen 通道归 OCV 原生，
    # 把它的音色策略换成「全都支持指令」会让原生 Qwen 的校验失真（它本来会对
    # 不支持指令的音色报错）。MiMo 自己那条链路的放行由 mimo_engine 负责。
    from . import mimo_engine

    if not mimo_engine.enabled():
        module.voice_supports_instructions = _voice_supports_instructions  # type: ignore[attr-defined]
        module.DEFAULT_QWEN_VOICE = config.mimo_default_voice()  # type: ignore[attr-defined]
        log("已接管配置视图与音色策略（旧模式）：面板层覆盖 .env 参与 OCV 的凭据校验")
    else:
        log("已接管配置视图：面板层将覆盖 .env 参与 OCV 的凭据校验（MiMo 为独立引擎，qwen 通道保持原生）")

    # 后端进程是 OCV 里最合适的 shim 宿主：它与界面同生命周期，且不涉及
    # 分离进程的 job object 权限问题。放后台线程启动，不阻塞后端就绪。
    import threading

    threading.Thread(target=_host_shim_in_backend, name="ocv-cloud-shim-host", daemon=True).start()

    # Jet Hub 桥（Node 子进程）同样挂在后端进程上：既保证「与界面同生命周期」，
    # 又保证**账号池只有一个实例**（多进程会互相覆盖 state.json，
    # account-pool.ts 的正确性前提是进程内权威副本）。
    if config.jethub_enabled() and config.jethub_autostart():
        threading.Thread(target=_host_jethub_bridge, name="ocv-jethub-bridge-host", daemon=True).start()

    # 顺带把 ASR 冷启动成本提前付掉。模块 2 的依赖自检只有 90 秒超时，而
    # ctranslate2 会连带拉起 6.9 GB 的 torch CUDA DLL，冷加载经常超时 ——
    # 那和插件无关，但会打死整个任务。预热本身也是后台线程 + 一次性子进程。
    from . import asr_prewarm

    asr_prewarm.prewarm_async()


_reapply_scheduled = False


def _schedule_derived_state_reapply() -> None:
    """把派生状态自愈排到后台线程（每个进程只排一次）。

    独立成函数是为了能从 ``_patch_main`` 的最开头调用 —— 见那里的注释：
    自愈不能挂在 ``_project_config_values`` 补丁成功与否上面。
    """
    global _reapply_scheduled
    if _reapply_scheduled:
        return
    _reapply_scheduled = True
    import threading

    threading.Thread(target=_reapply_derived_state, name="ocv-cloud-reapply", daemon=True).start()


def _reapply_derived_state() -> None:
    """把被软件更新抹掉的派生注入物补回来（前端按钮 / ``.pth`` 锚点）。

    只在**后端进程**触发（``backend.app.main`` 的导入钩子），且默认开启、
    可经 ``CLOUD_STACK_AUTO_REINJECT=0`` 关掉。产物是幂等的标记块，
    没东西可修时一个文件都不碰。
    """
    try:
        if not config.auto_reinject():
            return
        from . import reapply

        report = reapply.repair()
    except Exception as exc:  # noqa: BLE001 - 自愈失败不能影响 OCV 启动
        debug(f"派生注入自愈未执行：{type(exc).__name__}: {exc}")
        return

    labels = {"anchor": ".pth 注入锚点", "frontend": "前端面板按钮"}
    for name in report.get("repaired", []):
        log(f"检测到软件更新抹掉了注入，已自动重建：{labels.get(name, name)}（刷新页面即可生效）")
    for err in report.get("errors", []):
        debug(f"派生注入自愈部分失败：{err}")


def _host_shim_in_backend() -> None:
    from . import shim_launcher

    if shim_launcher.is_listening():
        return
    if shim_launcher.ensure_in_process():
        log(f"本地图片 shim 已由后端进程托管：{config.shim_base_url()}")
    else:
        debug("后端未能托管 shim（端口被占用或未开启自动启动）")


def _host_jethub_bridge() -> None:
    """把 Jet Hub 桥（Node 子进程）挂到后端进程上。

    为什么是**进程内守护线程 + 普通子进程**，而不是分离进程：
    与 shim 同样的理由（见 ``shim_launcher`` 模块文档）—— 受限环境里
    ``CREATE_BREAKAWAY_FROM_JOB`` 会被拒绝，分离出去的产物是僵尸。

    失败**只记日志**：桥起不来不应影响 OCV 启动，用户仍可用其它 provider。
    """
    try:
        from . import jethub_bridge

        if not config.jethub_enabled():
            debug("Jet Hub 已关闭，跳过桥托管")
            return
        error = jethub_bridge.ensure_running()
        if error:
            log(f"Jet Hub 桥未能启动：{error}（Jet Hub 模型将不可用，其余功能不受影响）")
        else:
            log(f"Jet Hub 桥已由后端进程托管：{jethub_bridge.bridge_base_url()}")
    except Exception as exc:  # noqa: BLE001 - 桥故障不得影响 OCV
        debug(f"Jet Hub 桥托管异常：{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# 4) 环境变量别名
# --------------------------------------------------------------------------

def apply_env_aliases() -> None:
    """让 OCV 的 DashScope 校验/状态灯在 MiMo 模式下也能通过。

    ``module1_agent_director.step2_qwen_synthesize`` 和 ``main.py`` 的
    请求校验都会检查 ``DASHSCOPE_API_KEY``。这里用 MiMo 的 Key 顶上，
    避免为了绕过一个存在性检查去改源码。

    注意这层对**长驻的 backend 进程**是不够的：它只在进程启动时执行一次，
    而 MIMO_API_KEY 往往是启动之后才在面板里填的。真正的兜底是上面的
    ``_patch_main``，它在每次请求时实时读取面板层。
    """
    if os.getenv("DASHSCOPE_API_KEY", "").strip():
        return
    key = config.mimo_api_key()
    if key:
        os.environ["DASHSCOPE_API_KEY"] = key
        debug("已用 MIMO_API_KEY 填充 DASHSCOPE_API_KEY（仅用于通过存在性校验）")


# --------------------------------------------------------------------------
# 5) Agent 1B：语义边界细化的健壮性（详见 agent1b_resilience 模块文档）
# --------------------------------------------------------------------------

def _patch_story_agents(module: object) -> None:
    """给 Agent 1B 补上「失败不致命」的降级路径。

    ``story_agents`` 是 OCV 的顶层模块（不是 ``backend.app`` 包内），
    ``pipeline.py`` 通过模块属性调用 ``refine_risky_semantic_units``，
    因此替换模块属性即可生效，无需改 OCV 源码。

    该补丁的价值见 ``agent1b_resilience`` 模块文档：原生实现会在
    「父单元只含 1 个超长 slide」时让模型返回重复区间，从而被
    ``_normalize_semantic_units`` 判为空、再被升级为致命错误终止整条流水线。
    """
    from . import agent1b_resilience

    agent1b_resilience.install(module)


# --------------------------------------------------------------------------
# 6) 动态视频阶段方案：reference_beat 记账字段的健壮性
#    （详见 motion_plan_resilience 模块文档）
# --------------------------------------------------------------------------

def _patch_video_motion_plan(module: object) -> None:
    """给动态视频的 motion_plan 校验补上「记账字段不致命」。

    原生 ``video_motion_plan.normalize_motion_plan`` 把 ``reference_beat``
    （核心参考图对应第几个阶段，一个 LLM 产出的**序号**）当硬契约校验，
    越界/类型漂移就抛 ``核心参考图对应的阶段编号无效``；而
    ``video_agents.direct_motion`` 只重试一次就升级为致命错误，
    **终止整条动态视频生产**。同一个字段还被当作数组下标用，不修会 IndexError。

    这里只包裹（wrap），不复制任何业务常量：真正"怎么算合格"仍由原生实现决定。

    ``install()`` 不传参数时会把它装到**所有**已导入的调用方模块上 ——
    因为 ``video_agents``/``video_plan``/``video_prompt_refresh`` 都用
    ``from .video_motion_plan import normalize_motion_plan`` 把函数绑进了
    自己的命名空间，只换定义处是没用的。

    ⚠ **顺序陷阱**：``backend.app.video_agents`` 会 ``import video_motion_plan``，
    于是 ``video_motion_plan`` 先完成 exec、再轮到 ``video_agents``；
    但 ``video_agents`` 的 exec 中执行 ``from ... import`` 时，我们可能**还没**
    走到 ``video_motion_plan`` 的补丁（``_patch_if_ready`` 是在各自 exec 完成后
    才触发）。因此这里除了装自己，还要**回头补齐**已经导入的调用方模块 ——
    否则 ``video_agents`` 手里会攥着原生函数，补丁形同不存在。
    """
    from . import motion_plan_resilience

    motion_plan_resilience.install()
    # 回头补齐：任何已经导入的调用方都要换成 wrapper。
    motion_plan_resilience.install(
        *(sys.modules[name] for name in motion_plan_resilience._TARGET_MODULES
          if name != "backend.app.video_motion_plan" and sys.modules.get(name) is not None)
    )


def _patch_video_agents(module: object) -> None:
    """``video_agents`` 模块体执行完后，把 F-010 的包装补上（含 ``_assemble_video_body``）。

    ## 为什么 ``video_agents`` 也必须是一个补丁目标

    F-010 要包装的 ``_assemble_video_body`` 是 ``video_agents`` 的**私有函数**，
    定义位置在模块**靠后**处（先有 ``write_video_prompts``，之后才是它）。
    而补丁的触发时机是"上游目标模块执行完"——``video_agents`` 依赖
    ``video_motion_plan``，所以 ``_patch_video_motion_plan`` 先跑，
    那一刻 ``_assemble_video_body`` **还不存在**。

    只靠 ``on_import`` 里的 ``retry_pending()`` 重试**不可靠**：它取决于
    之后还有没有别的 import 发生，而链路里可能很久没有（实测：全链路跑通时
    包装始终没装上，``wrapped_assemble`` 为空）。

    把 ``video_agents`` 本身登记为补丁目标后，``_PatchingLoader`` 会在它的
    **模块体完全执行完之后**回调这里 —— 那是确定性的、属性必然已存在的时机。
    """
    from . import motion_plan_resilience

    motion_plan_resilience.install()


def _patch_video_model_config(module: object) -> None:
    """把 OCV 的原生视频门禁桥接到插件面板的 Agnes 配置（F-011）。

    见 ``video_gate_bridge`` 模块文档：前端凭 ``/api/video-model`` 的
    ``source=='dedicated' && has_api_key`` 决定放不放行，而它只读
    ``VIDEO_API_*``，不认识插件的 ``AGNES_API_KEY`` —— 于是用户在面板里
    配好了 Agnes，界面仍然提示「请先配置视频 API」。
    """
    from . import video_gate_bridge

    video_gate_bridge.install()


def _patch_video_generation(module: object) -> None:
    """``video_generation`` 用 ``from .video_model_config import load_config``
    把函数对象绑进了自己的命名空间 ⇒ 只换定义处没用，必须连它一起换（铁律 29）。

    另外这个模块自己也登记在 ``video_gate_bridge._TARGET_MODULES`` 里，
    这里显式补一次是为了确保**属性已存在时立刻装上**，不依赖安装顺序。
    """
    from . import video_gate_bridge

    video_gate_bridge.install()


# --------------------------------------------------------------------------
# 目标注册表
# --------------------------------------------------------------------------

_TARGETS: dict[str, object] = {
    "backend.app.qwen_tts": _patch_qwen_tts,
    "backend.app.gemini_client": _patch_gemini_client,
    "backend.app.main": _patch_main,
    "backend.app.video_motion_plan": _patch_video_motion_plan,
    # F-010：必须在 video_agents **模块体跑完**后补它的私有函数
    "backend.app.video_agents": _patch_video_agents,
    # F-011：视频门禁桥接（含 from-import 绑定陷阱的那个调用方）
    "backend.app.video_model_config": _patch_video_model_config,
    "backend.app.video_generation": _patch_video_generation,
    "story_agents": _patch_story_agents,
}


def _is_initializing(target: Any) -> bool:
    """模块体还在执行中吗？

    CPython 在 ``exec_module`` **之前**就把模块放进 ``sys.modules``，
    所以模块体执行期间触发的任何钩子看到的都是"半成品"。此刻改掉的属性
    会被模块体后面那些 ``def`` / 赋值语句原样覆盖回去 —— 这是最容易踩空
    的一步，必须显式排除。
    """
    spec = getattr(target, "__spec__", None)
    return bool(getattr(spec, "_initializing", False))


def _patch_if_ready(module_name: str) -> None:
    if module_name in _PATCHED:
        return
    patcher = _TARGETS.get(module_name)
    if patcher is None:
        return
    target = sys.modules.get(module_name)
    if target is None:
        return
    if _is_initializing(target):
        # 交给 _PatchingLoader.exec_module 在模块体跑完后处理
        return
    if _ATTEMPTS.get(module_name, 0) >= _MAX_ATTEMPTS:
        return
    _ATTEMPTS[module_name] = _ATTEMPTS.get(module_name, 0) + 1

    # 先占位再执行。补丁函数内部会触发新的 import（例如延迟导入 mimo_tts），
    # 那个 import 又会回调到这里；不先占位就会自我递归。
    _PATCHED.add(module_name)
    try:
        patcher(target)  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001 - 插件绝不能拖垮 OCV
        log(f"补丁 {module_name} 失败，将保持 OCV 原生行为：{type(exc).__name__}: {exc}")
        if config.debug():
            import traceback

            log(traceback.format_exc())
        # 撤销占位，允许后续再试一次（最多 _MAX_ATTEMPTS 次）
        _PATCHED.discard(module_name)


# --------------------------------------------------------------------------
# 模块加载钩子：保证补丁一定发生在模块体执行完之后
# --------------------------------------------------------------------------

class _PatchingLoader:
    """包一层目标模块的 loader，在 ``exec_module`` 之后才打补丁。"""

    def __init__(self, inner: Any, fullname: str) -> None:
        self._inner = inner
        self._fullname = fullname

    def create_module(self, spec: Any) -> Any:
        factory = getattr(self._inner, "create_module", None)
        return factory(spec) if callable(factory) else None

    def exec_module(self, module: Any) -> None:
        self._inner.exec_module(module)
        # 到这一步模块体已完全执行，属性不会再被覆盖
        try:
            _patch_if_ready(self._fullname)
        except Exception:  # noqa: BLE001
            pass

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


class _PatchingFinder:
    """只在 ``sys.meta_path`` 里拦截两个目标模块，其余一律放行。"""

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname not in _TARGETS:
            return None
        if fullname in _PATCHED:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _PatchingLoader(spec.loader, fullname)
        return spec


def install_meta_hook() -> None:
    if any(isinstance(finder, _PatchingFinder) for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _PatchingFinder())
    debug("模块加载钩子已安装（补丁将在模块体执行完成后生效）")


def has_pending() -> bool:
    """还有没打完的补丁吗？导入钩子用它做快速短路。"""
    return any(
        name not in _PATCHED and _ATTEMPTS.get(name, 0) < _MAX_ATTEMPTS
        for name in _TARGETS
    )


def patched_modules() -> set[str]:
    return set(_PATCHED)


def patch_everything_ready() -> None:
    """把已经导入过的目标模块补上补丁。"""
    if not has_pending():
        return
    for name in _TARGETS:
        _patch_if_ready(name)


def on_import(module_name: str | None) -> None:
    """导入钩子的回调：只关心目标模块。"""
    # ⚠ 不能因为 has_pending() 为假就整体 return ——
    #   F-010 的延后重试发生在**所有目标模块都已打过补丁之后**
    #   （那时 has_pending() 已经是 False）。实测踩到：首版把它放在
    #   早退之后，retry_pending() 永远没机会跑，_assemble_video_body
    #   始终装不上。
    if has_pending():
        if module_name and module_name in _TARGETS:
            _patch_if_ready(module_name)
        # 相对导入（from .qwen_tts import ...）拿不到绝对名，用 sys.modules 兜底
        patch_everything_ready()
    # F-010：``_assemble_video_body`` 在 video_agents 模块体靠后位置才定义，
    # 而补丁由上游 video_motion_plan 触发 —— 首次必然拿不到这个属性。
    # 每次 import 都重试一次，直到它出现（有上限，不会永久轮询）。
    #
    # ⚠ 必须用 ``sys.modules.get`` 而不是 ``from . import ...``：
    #   on_import 会在**本模块自己正在被导入时**被回调，
    #   此时 `from . import motion_plan_resilience` 会命中"半成品"模块
    #   ⇒ AttributeError: partially initialized module ... has no attribute 'install'
    #   （实测踩到，插件直接注入失败）。已在 sys.modules 里且**已完全初始化**
    #   才调用它的 retry_pending。
    pending_module = sys.modules.get("ocv_cloud_stack.motion_plan_resilience")
    retry = getattr(pending_module, "retry_pending", None) if pending_module else None
    if callable(retry):
        try:
            retry()
        except Exception:  # noqa: BLE001 - 插件绝不能拖垮 OCV
            pass
