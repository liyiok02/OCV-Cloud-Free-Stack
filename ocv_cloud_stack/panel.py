"""配置面板的后端逻辑。

面板由 ``image_shim`` 顺带提供（同一台本地 HTTP 服务，多挂几个路由），
所以不依赖 OCV 的后端进程，也不会和它抢端口。

写回目标只有一个：``plugins/cloud_free_stack/var/config.json``。
**不碰 .env**——.env 是 install 时留下的基线，面板是运行期的覆盖层，
两者分开之后，"面板改完立刻生效"和"卸载时干净还原"才能同时成立。

只读 ``.env`` 里的原生 OCV 键（``IMAGE_API_BASE_URL`` 等），用于状态展示。
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import config, mimo_tts, models_catalog, sense_image

# --------------------------------------------------------------------------
# MiMo 预置音色
# --------------------------------------------------------------------------

MIMO_VOICES: list[dict[str, str]] = [
    {"value": "冰糖", "label": "冰糖 · 中文女声（默认）", "lang": "zh"},
    {"value": "茉莉", "label": "茉莉 · 中文女声", "lang": "zh"},
    {"value": "苏打", "label": "苏打 · 中文男声", "lang": "zh"},
    {"value": "白桦", "label": "白桦 · 中文男声", "lang": "zh"},
    {"value": "Mia", "label": "Mia · English female", "lang": "en"},
    {"value": "Chloe", "label": "Chloe · English female", "lang": "en"},
    {"value": "Milo", "label": "Milo · English male", "lang": "en"},
    {"value": "Dean", "label": "Dean · English male", "lang": "en"},
]

MIMO_MODELS: list[dict[str, str]] = [
    {"value": "mimo-v2.5-tts", "label": "mimo-v2.5-tts（预置音色，长视频首选）"},
    {"value": "mimo-v2.5-tts-voicedesign", "label": "mimo-v2.5-tts-voicedesign（用文字描述造音色）"},
    {"value": "mimo-v2.5-tts-voiceclone", "label": "mimo-v2.5-tts-voiceclone（上传参考音频克隆）"},
]

# 模型下拉的静态回退：只有在「服务商模型清单还没读到过」时才会显示。
# 一旦面板成功拉过一次，就会被真实清单替换（见 snapshot 里的 _model_options）。
#
# ⚠ 本表的每一项都必须**实测过当前可用**（2026-09-25 逐个验证，见 F-015）。
#   * `sensenova-6.7-flash-lite` 已实测 **HTTP 404 model is not found**，故剔除；
#   * `deepseek-v4.1-flash` 虽然 /models 会列出，但实测 **403 不在当前 token plan**，
#     因此这里**也不放**（宁可少给，也不要让用户选到一个必然失败的）。
SENSENOVA_LLM_FALLBACK: list[dict[str, str]] = [
    {"value": "deepseek-v4-pro", "label": "deepseek-v4-pro　1M 上下文 · 推理　★ 推荐"},
    {"value": "deepseek-v4-flash", "label": "deepseek-v4-flash　1M 上下文 · 推理"},
    {"value": "glm-5.2", "label": "glm-5.2　1M 上下文 · 输出 128K · 推理"},
    {"value": "kimi-k3", "label": "kimi-k3　1M 上下文 · 推理"},
    {"value": "sensenova-6.8-flash-lite", "label": "sensenova-6.8-flash-lite　256K 上下文 · 可读图"},
]

SENSENOVA_IMAGE_FALLBACK: list[dict[str, str]] = [
    {"value": "sensenova-u1.5-lite", "label": "sensenova-u1.5-lite　★ 推荐"},
    {"value": "sensenova-u1.5-fast", "label": "sensenova-u1.5-fast　更快"},
    {"value": "sensenova-u1-fast", "label": "sensenova-u1-fast（不支持图生图）"},
]

# 云端 OAuth 支持的提供商。
#
# **这里的顺序就是面板左栏的顺序**（面板按此渲染提供商列表）。
# 每一项都必须与 `jethub/runtime.mjs` 的 `PROVIDER_SPECS` / `CODEARTS_SPEC` /
# `ATOMCODE_SPEC` 里的 `id` 一致 —— `verify_jethub.py` 会做一致性断言。
JETHUB_PROVIDERS: list[dict[str, str]] = [
    {"value": "buddy", "label": "CodeBuddy（腾讯）"},
    {"value": "workbuddy", "label": "WorkBuddy（腾讯国际版）"},
    {"value": "lobsterai", "label": "LobsterAI（有道）"},
    {"value": "qoder", "label": "Qoder（阿里）"},
    {"value": "trae", "label": "TRAE（字节）"},
    {"value": "cline", "label": "Cline"},
    {"value": "loomy", "label": "Loomy（讯飞）"},
    {"value": "codearts", "label": "CodeArts（华为云）"},
    {"value": "atomcode", "label": "AtomCode（AtomGit）"},
]

# 只有实测支持 edits 的型号。`sensenova-u1-fast` 故意不在其中：
# 它对 /images/edits 返回 400 "model does not support image editing"。
SENSENOVA_IMAGE_EDIT_FALLBACK: list[dict[str, str]] = [
    {"value": "sensenova-u1.5-lite", "label": "sensenova-u1.5-lite　★ 推荐"},
    {"value": "sensenova-u1.5-fast", "label": "sensenova-u1.5-fast　更快"},
]

# OCV 前端 qwenVoiceGroups 的原样快照。前端是硬编码的，插件不改前端，
# 所以这里复制一份用于面板展示与映射。
OCV_VOICE_GROUPS: list[dict[str, Any]] = [
    {
        "label": "推荐叙述 · 支持配音描述",
        "voices": [
            ("Elias", "墨讲师 · 女性讲述感（默认）"),
            ("Eldric Sage", "沧明子 · 沉稳睿智老者"),
            ("Vincent", "田叔 · 沙哑烟嗓男声"),
            ("Neil", "阿闻 · 新闻主持男声"),
            ("Arthur", "徐大爷 · 沧桑老者"),
            ("Seren", "小婉 · 舒缓女声"),
            ("Maia", "四月 · 知性温柔女声"),
            ("Serena", "苏瑶 · 温柔自然女声"),
        ],
    },
    {
        "label": "其他普通话 · 支持配音描述",
        "voices": [
            ("Cherry", "芊悦 · 阳光亲切女声"),
            ("Ethan", "晨煦 · 温暖活力男声"),
            ("Chelsie", "千雪 · 二次元女友"),
            ("Momo", "茉兔 · 撒娇搞怪女声"),
            ("Vivian", "十三 · 可爱小暴躁女声"),
            ("Moon", "月白 · 率性帅气男声"),
            ("Kai", "凯 · 温柔耳语男声"),
            ("Nofish", "不吃鱼 · 设计师男声"),
            ("Bella", "萌宝 · 萝莉女声"),
            ("Mia", "乖小妹 · 温顺女声"),
            ("Mochi", "沙小弥 · 小大人男声"),
            ("Bellona", "燕铮莺 · 洪亮鲜活女声"),
            ("Bunny", "萌小姬 · 小萝莉女声"),
            ("Nini", "邻家妹妹 · 亲切女声"),
            ("Pip", "顽屁小孩 · 男童声"),
            ("Stella", "少女阿月 · 少女声"),
        ],
    },
    {
        "label": "国际音色 · 仅基础合成（不填配音描述）",
        "voices": [
            ("Jennifer", "詹妮弗 · 电影感美语女声"),
            ("Ryan", "甜茶 · 戏感美语男声"),
            ("Katerina", "卡捷琳娜 · 成熟御姐"),
            ("Aiden", "艾登 · 美语大男孩"),
            ("Bodega", "博德加 · 西班牙语男声"),
            ("Sonrisa", "索尼莎 · 拉美女声"),
            ("Alek", "阿列克 · 俄语男声"),
            ("Dolce", "多尔切 · 意大利语男声"),
            ("Sohee", "素熙 · 韩语女声"),
            ("Ono Anna", "小野杏 · 日语女声"),
            ("Lenn", "莱恩 · 德语男声"),
            ("Emilien", "埃米尔安 · 法语男声"),
            ("Andre", "安德雷 · 磁性沉稳男声"),
            ("Radio Gol", "拉迪奥·戈尔 · 男声"),
        ],
    },
    {
        "label": "方言音色 · 仅基础合成（不填配音描述）",
        "voices": [
            ("Jada", "上海-阿珍 · 上海女声"),
            ("Dylan", "北京-晓东 · 北京男声"),
            ("Li", "南京-老李 · 南京男声"),
            ("Marcus", "陕西-秦川 · 陕西男声"),
            ("Roy", "闽南-阿杰 · 闽南男声"),
            ("Peter", "天津-李彼得 · 天津男声"),
            ("Sunny", "四川-晴儿 · 四川女声"),
            ("Eric", "四川-程川 · 四川男声"),
            ("Rocky", "粤语-阿强 · 粤语男声"),
            ("Kiki", "粤语-阿清 · 粤语女声"),
        ],
    },
]


def clone_voice_rows() -> list[dict[str, Any]]:
    """克隆音色列表（供映射下拉与克隆管理卡片使用）。"""
    rows: list[dict[str, Any]] = []
    for name, meta in sorted(config.clone_voices().items()):
        missing = not (config.clone_voices_dir() / meta["file"]).is_file()
        rows.append(
            {
                "value": name,
                "bytes": meta["bytes"],
                "mime": meta["mime"],
                "uploaded_at": meta["uploaded_at"],
                "missing": missing,
            }
        )
    return rows


def ocv_voice_rows() -> list[dict[str, Any]]:
    """面板音色映射表的数据行。"""
    current = config.voice_map()
    rows: list[dict[str, Any]] = []
    for group in OCV_VOICE_GROUPS:
        for value, label in group["voices"]:
            rows.append(
                {
                    "value": value,
                    "label": label,
                    "group": group["label"],
                    "target": current.get(value, ""),
                }
            )
    return rows


# --------------------------------------------------------------------------
# 字段 schema
# --------------------------------------------------------------------------

FIELDS: list[dict[str, Any]] = [
    # ---- MiMo 配音 ----
    {
        "key": "MIMO_API_KEY",
        "group": "mimo",
        "label": "MiMo API Key",
        "type": "password",
        "secret": True,
        "required": True,
        "placeholder": "sk-...",
        "help": "小米 MiMo 开放平台的密钥。1.10.0 起 MiMo 是**独立的配音引擎**：在 OCV 的「执行方式」里选「MiMo TTS（云端免费栈）」，与原生「Qwen-TTS」两个选项同时可用、互不影响。填这里即可启用 MiMo 那条。",
    },
    {
        "key": "MIMO_TTS_BASE_URL",
        "group": "",
        "default": "https://api.xiaomimimo.com/v1",
        "label": "服务地址",
        "type": "text",
        "placeholder": "https://api.xiaomimimo.com/v1",
        "help": "官方默认 https://api.xiaomimimo.com/v1，通常不用改。",
    },
    {
        "key": "MIMO_TTS_MODEL",
        "group": "",
        "default": "mimo-v2.5-tts",
        "label": "模型",
        "type": "select",
        "options": MIMO_MODELS,
        "allow_custom": True,
        "dynamic_models": "tts",
        "credentials": {"api_key": "MIMO_API_KEY", "api_base": "MIMO_TTS_BASE_URL"},
        "placeholder": "mimo-v2.5-tts",
        "help": "面板会自动读取 MiMo 当前可用的模型清单，点下拉直接选。长视频用 mimo-v2.5-tts；voiceclone 需要每次请求内联参考音频，长文会明显变慢。",
    },
    {
        "key": "MIMO_TTS_VOICE",
        "group": "",
        "default": "冰糖",
        "label": "默认音色",
        "type": "select",
        "options": MIMO_VOICES,
        "allow_custom": True,
        "placeholder": "冰糖",
        "help": "当某句的音色没在下面的映射表里命中时使用。",
    },
    {
        "key": "MIMO_TTS_RETRIES",
        "group": "",
        "default": "3",
        "label": "单句重试次数",
        "type": "number",
        "min": 1,
        "max": 10,
        "placeholder": "3",
        "help": "只对网络/限流类错误重试；鉴权与参数错误会直接失败，不空转。",
    },
    # ---- 语言模型（OpenAI 兼容）----
    # ---- 语言模型：来源选择（互斥） ----
    #
    # 用户明确要求：「语言模型和云端 OAuth 两项都是 LLM，不能同时使用，
    # 在语言模型中给用户下拉选择，是自行用 API 还是云端 OAuth」。
    #
    # 所以这里放一个**总闸**字段：它决定 `LANGUAGE_PROVIDER` 最终取
    # `sensenova`（自行用 API）还是 `jethub`（云端 OAuth）。
    # 下面的 `_source_controls_visibility()` 会用同一个值把无关字段置灰/隐藏，
    # 保证界面上**一眼看出当前用的是哪一路**、且不会误填另一路的配置。
    # ---- 语言模型：来源二选一（互斥） ----
    #
    # 用户明确要求：「语言模型和云端 OAuth 两项都是 LLM，不能同时使用，
    # 在语言模型中给用户下拉选择，是自行用 API 还是云端 OAuth」，
    # 且「语言模型应可选择使用云端 OAuth 中的**具体哪个模型**；
    # 云端 OAuth 页只用于配置账号等功能，不用于选择具体用哪个模型」。
    #
    # 所以：模型来源与两路的模型下拉**都留在这一页**；下方 `_source_of()`
    # 会给每个字段打 `source` 标记，前端据此只显示当前来源相关的字段。
    {
        "key": "LLM_SOURCE",
        "group": "llm",
        "label": "模型来源",
        "type": "select",
        "source": "both",
        "options": [
            {"value": "custom", "label": "自行填写 API（任意 OpenAI 兼容服务商）"},
            {"value": "jethub", "label": "云端 OAuth（CodeBuddy / Qoder / TRAE 等免费额度）"},
        ],
        "default": "custom",
        "restart": False,
        "help": (
            "**两路互斥，只能选一个。** 选「自行填写 API」用下面的服务地址 / Key / 模型；"
            "选「云端 OAuth」改用本机桥 —— 那时下面的地址 / Key / 商汤模型会被忽略，"
            "改由「云端 OAuth 模型」下拉决定用哪个。保存即生效，无需重启 OCV。"
        ),
    },
    {
        "key": "SENSENOVA_API_KEY",
        "group": "",
        "label": "API Key",
        "type": "password",
        "secret": True,
        "required": True,
        "source": "custom",
        "placeholder": "sk-...",
        "help": (
            "所选服务商的密钥。仅当「模型来源 = 自行填写 API」时使用。"
            "面板里留空不遮蔽 .env 的值。"
        ),
    },
    {
        "key": "SENSENOVA_API_BASE",
        "group": "",
        "default": "https://token.sensenova.cn/v1",
        "label": "服务地址",
        "type": "text",
        "source": "custom",
        "placeholder": "https://token.sensenova.cn/v1",
        "help": "任何 OpenAI 兼容的接口地址。默认商汤 token.sensenova.cn/v1；接其它服务商时整体替换（如 https://api.deepseek.com/v1），并同步换 Key 与模型。",
    },
    {
        "key": "SENSENOVA_MODEL",
        "group": "",
        "default": "deepseek-v4-pro",
        "label": "模型",
        "type": "select",
        "source": "custom",
        "options": SENSENOVA_LLM_FALLBACK,
        "allow_custom": True,
        "dynamic_models": "llm",
        "credentials": {"api_key": "SENSENOVA_API_KEY", "api_base": "SENSENOVA_API_BASE"},
        "placeholder": "deepseek-v4-pro",
        "help": (
            "打开面板时会按上面的服务地址与 Key 自动读取可用模型，点下拉直接选（下拉里带上下文长度与能力标注）。"
            "若频繁遇到 429 限流或 ReadTimeout，换更快的模型可明显降低单次耗时与并发压力。"
        ),
    },
    # ---- 云端 OAuth 的模型选择（**放在语言模型页**，按用户要求） ----
    {
        "key": "JETHUB_MODEL",
        "group": "",
        "default": "",
        "label": "云端 OAuth 模型",
        "type": "select",
        "source": "jethub",
        "options": [],
        "allow_custom": True,
        "dynamic_models": "llm_jethub",
        "placeholder": "（先在「云端 OAuth」分页登录账号）",
        "help": (
            "**这里选云端 OAuth 实际用哪个模型。** 清单聚合了全部**已登录**提供商的可用模型，"
            "每项形如「CodeBuddy（腾讯） · DeepSeek V4 Flash」—— 前缀标明它属于哪家，"
            "因为不同提供商的模型 id 会重名（`deepseek-v4-flash` 在好几家都有）。"
            "卡顶部的提示条会显示**当前选中的是哪一个**。"
            "**必须先登录账号**，否则这里是空的。"
        ),
    },
    {
        "key": "SENSENOVA_MAX_TOKENS",
        "group": "",
        "default": "0",
        "label": "最大输出 token",
        "type": "number",
        "source": "custom",
        "min": 0,
        "max": 393216,
        "placeholder": "0（不限制）",
        "help": (
            "**留 0 = 不限制，交给模型自身的默认值**（默认，推荐）。"
            "服务商的 /models 声明**不可信**（实测 deepseek-flash 声明 65536、真实 393216，"
            "少报 6 倍，且每个模型都不同），端点没有真值可读，所以"
            "「按模型自动」的唯一可靠做法就是**不指定**。"
            "实测：不指定时上游按模型自身默认执行（deepseek-flash 给 65536，是旧默认 16384 的 4 倍）。"
            "填正整数则作为**下限**生效（低于它会被抬到该值），"
            "留给需要精确控制成本/时长的场景。"
        ),
    },
    {
        "key": "CLOUD_STACK_LLM_THINKING",
        "group": "",
        "default": "off",
        "label": "深度思考",
        "type": "select",
        "options": [
            {"value": "off", "label": "关闭（默认 · 推荐）"},
            {"value": "low", "label": "低"},
            {"value": "medium", "label": "中"},
            {"value": "high", "label": "高"},
        ],
        "help": (
            "控制语言模型的思考（reasoning）开销。默认**关闭**：画面规划类任务要的是"
            "稳定、可解析的 JSON，思考内容只会白占输出预算并拖长单次耗时。"
            "只有换用强推理模型、且发现分组质量明显不足时才考虑开启。"
            "已实测：关闭时上游返回的 reasoning_content 长度为 0。"
            "（此项会自动合并进请求，无需再手写下面的扩展参数。）"
        ),
    },
    {
        "key": "CLOUD_STACK_LLM_EXTRA_BODY",
        "group": "",
        "default": "",
        "label": "扩展请求参数（JSON）",
        "type": "text",
        "placeholder": '{"top_k": 20}',
        "help": (
            "以 JSON 对象形式合并进语言模型请求根部，用于服务商特有的参数"
            "（如 top_k、enable_search）。一般留空即可。"
            "注意：`reasoning_effort` / `thinking` 两个键由上面的「深度思考」"
            "统一管；当「深度思考」不是「关闭」时，这里写的这两个键会被忽略，"
            "以免出现「面板显示已开启、请求里却仍是 none」的假象。"
        ),
    },
    {
        "key": "CLOUD_STACK_LLM_RETRY_ENABLED",
        "group": "",
        "default": "1",
        "label": "阶梯重试（30 档）",
        "type": "select",
        "options": [
            {"value": "1", "label": "开启（默认 · 推荐）"},
            {"value": "0", "label": "关闭（恢复 OCV 原生 3 次）"},
        ],
        "help": (
            "遇到 429 限流 / 5xx / 超时等**上游明确可重试**的错误时，按阶梯退避自动重发，"
            "档位与云端视频**完全一致**：1-3 次每 1 秒、4-8 次每 5 秒、9-12 次每 10 秒、"
            "13-15 次每 15 秒、16-20 次每 30 秒、21-25 次每 45 秒、26-30 次每 1 分钟"
            "（合计约 13 分钟）。每次等待再叠加 ±30% 随机抖动，"
            "避免多个阶段进程同时重发造成惊群。"
            "OCV 原生只有 3 次、间隔 3s 起、30s 就封顶，遇到持续限流熬不过去。"
            "鉴权与参数类错误**不会**重试，照旧立刻失败并报出真实原因，所以开启它不会掩盖故障。"
            "重试在**当前阶段进程内**同步进行，最坏情况下该次模型调用会多等上述时长。"
        ),
    },
    {
        "key": "CLOUD_STACK_LLM_RETRY_COUNT",
        "group": "",
        "default": "30",
        "label": "重试次数上限",
        "type": "number",
        "min": 0,
        "max": 60,
        "placeholder": "30",
        "help": (
            "不含首次请求的重试次数。0 = 不重试。档位按上表递增、1 分钟封顶，"
            "所以 30 次合计约 13 分钟（与云端视频默认一致）。"
            "429（tpm/rpm 限流）通常等几分钟就恢复，次数给足能显著提高夜间批量任务的通过率；"
            "代价是该阶段失败暴露得更晚。"
        ),
    },
    {
        "key": "CLOUD_STACK_AGENT1B_RESILIENT",
        "group": "",
        "default": "1",
        "label": "Agent 1B 容错",
        "type": "select",
        "options": [
            {"value": "1", "label": "开启（默认 · 推荐）"},
            {"value": "0", "label": "关闭（恢复严格终止）"},
        ],
        "help": (
            "Agent 1B（语义边界副导演）细化失败时是否继续。开启时：细化失败则保留"
            "原单元继续渲染，不再中断整个任务（诊断里会记录 failed_units，日志也会提示）。"
            "关闭后恢复 OCV 原生行为——任一边界验收失败即终止任务。"
            "排查模型质量问题时才需要关闭。"
        ),
    },
    {
        "key": "CLOUD_STACK_AGENT1B_SKIP_INSEPARABLE",
        "group": "",
        "default": "1",
        "label": "Agent 1B 单镜跳过",
        "type": "select",
        "options": [
            {"value": "1", "label": "开启（默认 · 推荐）"},
            {"value": "0", "label": "关闭（照常送模型）"},
        ],
        "help": (
            "只覆盖单个 slide 的父单元**结构上不可能**有合法切分（子单元必须首尾相接、"
            "每个 slide 恰好消费一次，1 个 slide 切不出 2 段）。开启时直接跳过这类细化，"
            "省下一次注定失败的模型调用。这是纯优化，与上面的「容错」是**两个独立开关**："
            "关掉容错不会连带关掉它。仅在需要复现原生失败文本时才关闭。"
        ),
    },
    # ---- 图片模型（商汤）----
    {
        "key": "SENSENOVA_IMAGE_API_BASE",
        "group": "image",
        "label": "图片服务地址",
        "type": "text",
        "placeholder": "（留空=与语言模型相同）",
        "help": "留空即复用语言模型的服务地址。图片接口是商汤 /images 形态，接其它图片服务商前先确认接口兼容。",
    },
    {
        "key": "SENSENOVA_IMAGE_API_KEY",
        "group": "image",
        "label": "图片 API Key",
        "type": "password",
        "secret": True,
        "placeholder": "（留空=与语言模型共用）",
        "help": "图片走独立 Key（例如出图与文字用不同账号/额度）。留空则与语言模型共用上面那个 Key。",
    },
    {
        "key": "SENSENOVA_IMAGE_MODEL",
        "group": "",
        "default": "sensenova-u1.5-lite",
        "label": "文生图模型",
        "type": "select",
        "options": SENSENOVA_IMAGE_FALLBACK,
        "allow_custom": True,
        "dynamic_models": "image",
        "credentials": {"api_key": "SENSENOVA_IMAGE_API_KEY", "api_base": "SENSENOVA_IMAGE_API_BASE"},
        "placeholder": "sensenova-u1.5-lite",
        "help": "按图片服务地址与图片 Key 自动读取可用的出图模型（按输出模态筛选），点下拉直接选。",
    },
    {
        "key": "SENSENOVA_IMAGE_EDIT_MODEL",
        "group": "image",
        "label": "图生图模型",
        "type": "select",
        "options": SENSENOVA_IMAGE_EDIT_FALLBACK,
        "allow_custom": True,
        # 图生图是**独立用途**：既要按输出模态筛，还要剔除实测不支持 edits 的型号
        # （如 sensenova-u1-fast 会返回 "model does not support image editing"）。
        "dynamic_models": "image_edit",
        "credentials": {"api_key": "SENSENOVA_IMAGE_API_KEY", "api_base": "SENSENOVA_IMAGE_API_BASE"},
        "placeholder": "（留空=与文生图相同）",
        "help": "画面修改区重绘走这个模型，需要支持参考图。留空即复用上面的文生图模型。",
    },
    {
        "key": "CLOUD_STACK_IMAGE_WATERMARK",
        "group": "image",
        "label": "保留商汤水印",
        "type": "bool",
        "placeholder": "0",
        "help": "商汤接口默认会打日日新水印，插件已默认关闭。",
    },
    {
        "key": "CLOUD_STACK_IMAGE_PROMPT_EXTEND",
        "group": "image",
        "label": "允许商汤扩写提示词",
        "type": "bool",
        "placeholder": "0",
        "help": "开着会让商汤改写 OCV 精心锁死的单镜头/画风约束，建议保持关闭。",
    },
    {
        "key": "CLOUD_STACK_IMAGE_MAX_REFERENCE",
        "group": "",
        "default": "3",
        "label": "参考图上限",
        "type": "number",
        "min": 1,
        "max": 4,
        "placeholder": "3",
        "help": "商汤建议多参考图控制在 2–3 张，过多会稀释主体。",
    },
    {
        "key": "CLOUD_STACK_IMAGE_WORKERS",
        "group": "",
        "default": "4",
        "label": "生图并发线程",
        "type": "number",
        "min": 1,
        "max": 16,
        "placeholder": "4",
    },
    # ---- 其它 ----
    {
        "key": "CLOUD_STACK_SHIM_PORT",
        "group": "misc",
        "default": "8799",
        "label": "本地 shim 端口",
        "type": "number",
        "min": 1024,
        "max": 65535,
        "placeholder": "8799",
        "restart": True,
        "help": "改动后需要重新运行「启动_云端免费栈.bat」并重启 OCV 才生效。",
    },
    {
        "key": "CLOUD_STACK_DEBUG",
        "group": "misc",
        "label": "调试日志",
        "type": "bool",
        "placeholder": "0",
        "help": "打开后补丁失败会打印完整 traceback。",
    },
    {
        "key": "CLOUD_STACK_MOTION_PLAN_RESILIENT",
        "group": "misc",
        "default": "1",
        "label": "动态方案容错",
        "type": "bool",
        "placeholder": "1",
        "help": (
            "动态视频的 reference_beat（核心参考图对应第几阶段）是模型产出的序号，"
            "越界或类型不对时会被夹紧到合法区间，而不是终止整条生产。"
            "置 0 回退 OCV 原生严格行为（仅用于对照排查）。"
        ),
    },
    # ---- 云端视频（Agnes AI）----
    {
        "key": "AGNES_API_KEY",
        "group": "video",
        "label": "Agnes API Key",
        "type": "password",
        "secret": True,
        "placeholder": "在 platform.agnes-ai.com 创建",
        "help": "云端视频走 Agnes AI（OpenAI 风格接口）。Key 只存本地面板层，不落 .env。",
    },
    {
        "key": "AGNES_VIDEO_MODEL",
        "group": "video",
        "default": "agnes-video-2.5-flash",
        "label": "视频模型",
        "type": "select",
        "allow_custom": True,
        "options": [
            {"value": "agnes-video-2.5-flash", "label": "agnes-video-2.5-flash　当前限时免费 ★"},
            {"value": "agnes-video-2.5", "label": "agnes-video-2.5　按秒计费（720P $0.025/秒）"},
            {"value": "agnes-video-v2.0", "label": "agnes-video-v2.0　2026-09-25 下线，勿用"},
        ],
        "help": "agnes-video-2.5-flash 当前限免，且只支持 720P、最多 5 张参考图、不支持参考视频。",
    },
    {
        "key": "AGNES_API_BASE",
        "group": "video",
        "default": "https://apihub.agnes-ai.com/v1",
        "label": "Agnes 接口地址",
        "type": "text",
        "placeholder": "https://apihub.agnes-ai.com/v1",
        "help": "国际站地址。一般不需要改。",
    },
    {
        "key": "CLOUD_STACK_VIDEO_ENABLED",
        "group": "video",
        "label": "启用云端视频接管",
        "type": "bool",
        "placeholder": "0",
        "help": (
            "默认关闭。开启前请先运行 cloud_stack_ctl.py video-install。"
            "视频按秒计费，本适配尚未经真实 Key 端到端验证，故默认不动你的视频配置。"
        ),
    },
    {
        "key": "CLOUD_STACK_VIDEO_MODE",
        "group": "video",
        "default": "auto",
        "label": "生成模式",
        "type": "select",
        "options": [
            {"value": "auto", "label": "auto　按参考图数量自动推导 ★"},
            {"value": "keyframe", "label": "keyframe　首帧/尾帧控制"},
            {"value": "reference", "label": "reference　素材参考"},
            {"value": "text", "label": "text　纯文生视频"},
        ],
        "help": (
            "auto 规则：0 张图=text；1 张=keyframe 首帧（对「让分镜图动起来」最忠实）；"
            "2 张=keyframe 首尾帧；3 张及以上=reference。"
        ),
    },
    {
        "key": "CLOUD_STACK_VIDEO_SIZE",
        "group": "video",
        "default": "720P",
        "label": "输出分辨率档位",
        "type": "select",
        "options": [
            {"value": "720P", "label": "720P　$0.025/秒 ★"},
            {"value": "1080P", "label": "1080P　$0.040/秒"},
            {"value": "1K", "label": "1K　$0.040/秒"},
            {"value": "2K", "label": "2K　$0.055/秒"},
        ],
        "help": "Agnes 没有 480p 档位；OCV 请求 480p 时也会按这里输出（并在日志中说明）。",
    },
    {
        "key": "CLOUD_STACK_VIDEO_INLINE_MEDIA",
        "group": "video",
        "default": "1",
        "label": "参考图内联为 data URI",
        "type": "bool",
        "placeholder": "1",
        "help": (
            "官方视频文档只承诺「公网可访问的 URL」并要求避免本地地址，"
            "但 OCV 只能给本地图。默认内联（Agnes 图像接口明确接受 data URI）；"
            "若上游拒绝，置 0 可切严格模式排查。"
        ),
    },
    {
        "key": "CLOUD_STACK_VIDEO_SUBMIT_RETRIES",
        "group": "video",
        "default": "30",
        "label": "提交重试次数",
        "type": "number",
        "placeholder": "30",
        "help": (
            "上游「队列满（503）/ 限流（429）」时最多重试几次，默认 30。"
            "这类拒绝意味着**任务没有被创建**，重试不会重复扣费；"
            "由插件在后台按**阶梯退避**自动重试，界面显示为排队中。"
            "节奏：1-3 次每 1 秒 → 4-8 次每 5 秒 → 9-12 次每 10 秒 → "
            "13-15 次每 15 秒 → 16-20 次每 30 秒 → 21-25 次每 45 秒 → "
            "26-30 次每 1 分钟（合计约 13 分钟）。"
            "「结果无法确认」的情况**从不**自动重试（可能已扣费）。"
        ),
    },
    # ---- Jet Hub（CodeBuddy 等免费 provider） ----
    {
        "key": "JETHUB_ENABLED",
        "group": "jethub",
        "label": "启用云端 OAuth",
        "type": "select",
        "options": [
            {"value": "1", "label": "启用（默认）"},
            {"value": "0", "label": "关闭"},
        ],
        "default": "1",
        "help": (
            "总开关。云端 OAuth 把 **CodeBuddy、Qoder、TRAE、Loomy** 等 9 家"
            "免费额度账号接进 OCV，当作 OpenAI 兼容服务商使用。"
            "关闭后本机桥不会被拉起，OCV 完全看不到它（对原生链路零影响）。"
        ),
    },
    # ⚠ `JETHUB_PROVIDER` **故意不作为字段出现在这里**。
    #
    # 用户要求：「有下面的账号管理窗口就不需要上面的提供商选择了，删掉」——
    # 因为「云端 OAuth」分页的账号卡有**提供商左栏**，那才是选提供商的入口；
    # 上面的字段卡再放一个同名下拉是重复的，且两处状态容易不同步。
    #
    # 该配置键**仍然存在且仍然生效**（保存左栏的选择时会写它），
    # 只是不再渲染成一张字段卡。`config.jethub_provider()` 照常读它。
    {
        "key": "JETHUB_BRIDGE_PORT",
        "group": "",
        "label": "桥端口",
        "type": "number",
        "default": "8801",
        "placeholder": "8801",
        "restart": True,
        "help": (
            "本机 OpenAI 兼容桥监听的端口。默认 8801，"
            "与 OCV 后端 8010、前端 5173、图片 shim 8799 都错开。"
            "**改端口需要重启 OCV 后端**。"
        ),
    },
    {
        "key": "JETHUB_AUTOSTART",
        "group": "",
        "label": "随 OCV 自动启动桥",
        "type": "select",
        "options": [
            {"value": "1", "label": "自动（默认）"},
            {"value": "0", "label": "手动"},
        ],
        "default": "1",
        "help": (
            "由 OCV 后端进程在启动时顺手拉起 Node 桥。"
            "关掉后可用下方「启动/重启桥」按钮手动控制，或跑 "
            "`python -m ocv_cloud_stack.jethub_bridge start`。"
        ),
    },
    {
        "key": "JETHUB_BRIDGE_TIMEOUT_MS",
        "group": "",
        "label": "单次请求软超时（毫秒）",
        "type": "number",
        "default": "110000",
        "placeholder": "110000",
        "help": (
            "默认 110000（110 秒），**刻意略低于 OCV 的 120 秒读超时**："
            "让桥有机会返回结构化的 503（OCV 会按可重试状态码处理），"
            "而不是让 OCV 拿到一个断掉的连接。"
        ),
    },
]

GROUP_LABELS: dict[str, str] = {
    "mimo": "MiMo 配音（必需）",
    "llm": "语言模型（OpenAI 兼容）",
    "image": "图片模型（商汤）",
    "video": "云端视频（Agnes）",
    "jethub": "云端 OAuth",
    "misc": "其它",
}

# 展示用的原生 OCV 键：面板只读，不写回 .env
READONLY_ENV_KEYS = (
    "LANGUAGE_PROVIDER", "IMAGE_API_BASE_URL", "IMAGE_MODEL_ID",
    "VIDEO_API_BASE_URL", "VIDEO_SUBMIT_PATH", "VIDEO_QUERY_PATH", "VIDEO_RESOLUTION",
)


def _default_of(field: dict[str, Any]) -> str:
    """字段在「面板与 .env 都为空」时实际会用的值，仅用于界面提示。

    刻意不拿 ``placeholder`` 兜底：密钥类字段的占位符是 ``sk-...``，
    那不是默认值，展示出来会让人以为自己已经配好了。
    """
    if field.get("secret"):
        return ""
    return str(field.get("default") or "")


def _resolve_groups() -> list[tuple[dict[str, Any], str]]:
    """解析字段分组，空值表示「跟随上一个显式分组」。

    FIELDS 里连续字段属于同一分组时不必重复写 ``group``，但**分组必须解析出
    真实名字** —— 前端是按 ``field.group === groupKey`` 精确过滤来选卡片的，
    留一个占位符过去，那些字段就一个都渲染不出来。
    """
    resolved: list[tuple[dict[str, Any], str]] = []
    current = ""
    for field in FIELDS:
        raw = str(field.get("group") or "").strip()
        if raw:
            current = raw
        resolved.append((field, current))
    return resolved


def _model_options(kind: str) -> list[dict[str, str]]:
    """把磁盘缓存里的模型清单转成下拉选项（不发网络请求）。

    转换逻辑放在 ``models_catalog.panel_options``，与拉取路径共用同一套
    「补齐漏报 + 按用途筛能力」规则 —— 否则修复前的陈旧缓存会让面板首屏
    重新显示错误的清单。
    """
    return models_catalog.panel_options(kind)


def snapshot() -> dict[str, Any]:
    """面板所需的全部状态。"""
    values = {field["key"]: config.get(field["key"]) for field in FIELDS}
    layers = {field["key"]: config.layer_of(field["key"]) for field in FIELDS}

    fields: list[dict[str, Any]] = []
    for field, group in _resolve_groups():
        item = dict(field)
        item["group"] = group
        item["group_label"] = GROUP_LABELS.get(group, group)
        item["default"] = _default_of(field)
        dynamic = str(field.get("dynamic_models") or "").strip()
        if dynamic:
            options = _model_options(dynamic)
            if options:
                # 缓存里有真实清单就直接当下拉选项，字段与选项同时到达，
                # 面板首屏就是可选的，不必等一次网络往返。
                item["options"] = options
            # 把「这份清单是怎么来的」一并下发：服务商 /models 会漏报，
            # 也会报出实际不支持某能力的型号，用户需要知道我们做过校正。
            item["model_note"] = models_catalog.verify_note(dynamic)
        fields.append(item)

    shim_env = config.get("IMAGE_API_BASE_URL")
    return {
        "fields": fields,
        "values": values,
        "layers": layers,
        "groups": [
            {"key": key, "label": label}
            for key, label in GROUP_LABELS.items()
        ],
        "voice_map": config.voice_map(),
        "ocv_voices": ocv_voice_rows(),
        "mimo_voices": MIMO_VOICES,
        "clone_voices": clone_voice_rows(),
        "model_cache": models_catalog.cache_info(),
        "readonly_env": {key: config.get(key) for key in READONLY_ENV_KEYS},
        # 语言模型来源（互斥开关）。前端据此**只显示当前来源相关字段** ——
        # 用户要求两路不能同时用，界面上也不该让两套配置并存使人误填。
        # `resolved_provider` 是最终落到 OCV 的 provider id，用于状态显示。
        "llm_source": config.llm_source(),
        "resolved_provider": config.resolved_language_provider(),
        "status": {
            "mimo_configured": bool(config.mimo_api_key()),
            "llm_configured": bool(config.sensenova_api_key()),
            "image_configured": bool(config.sensenova_image_api_key()),
            "agnes_configured": bool(config.video_api_key()),
            # 视频分页要展示的运行时状态（含 shim 侧任务计数）。
            # 取不到也不影响面板 —— shim 没起来时给一份纯配置视图。
            "video": _video_status(),
            "shim_base_url": config.shim_base_url(),
            "shim_port": config.shim_port(),
            "shim_matches_env": shim_env.rstrip("/") == config.shim_base_url(),
            "store_path": str(config.store_path()),
            "env_path": str(config.project_root() / ".env"),
        },
    }


def _video_status() -> dict[str, Any]:
    """视频分页的状态快照：配置层 +（若 shim 在跑）运行层。"""
    info: dict[str, Any] = {
        "enabled": config.video_enabled(),
        "model": config.video_model(),
        "base": config.video_base_url(),
        "size": config.video_size(),
        "mode": config.video_mode(),
        "inline_media": config.video_inline_media(),
    }
    try:
        import json
        import urllib.request

        url = f"{config.shim_base_url()}/api/video/status"
        with urllib.request.urlopen(url, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            info.update({k: v for k, v in data.items() if k not in {"model", "base", "size", "mode", "inline_media"}})
    except Exception:  # noqa: BLE001 - shim 未运行是常态，不是错误
        pass
    return info


def list_models(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """读取某用途下服务商当前可用的模型清单。

    允许带上**尚未保存**的 Key / 服务地址：用户往往是先填 Key 再想去挑模型，
    此时逼他先保存一次才给读清单，纯属白折腾。
    """
    body = payload or {}
    kind = str(body.get("kind") or "llm").strip().lower()
    result = models_catalog.list_models(
        kind,
        api_key=str(body.get("api_key") or "").strip(),
        api_base=str(body.get("api_base") or "").strip(),
        force=bool(body.get("force")),
    )
    return {
        "ok": bool(result.get("ok")),
        "kind": kind,
        "message": str(result.get("message") or ""),
        "api_base": result.get("api_base") or "",
        "cached": bool(result.get("cached")),
        "models": [
            {"value": str(row["value"]), "label": str(row.get("label") or row["value"])}
            for row in (result.get("models") or [])
        ],
    }


def _coerce(field: dict[str, Any], raw: Any) -> str:
    kind = str(field.get("type") or "text")
    text = str(raw if raw is not None else "").strip()
    if not text:
        return ""
    if kind == "bool":
        return "1" if text.lower() in {"1", "true", "yes", "on", "y"} else "0"
    if kind == "number":
        try:
            number = int(float(text))
        except (TypeError, ValueError):
            raise ValueError(f"{field['label']} 需要是数字")
        minimum = field.get("min")
        maximum = field.get("max")
        if isinstance(minimum, int):
            number = max(minimum, number)
        if isinstance(maximum, int):
            number = min(maximum, number)
        return str(number)
    return text


def _validate(field: dict[str, Any], value: str) -> None:
    if not value:
        return
    if field.get("type") == "select" and not field.get("allow_custom"):
        allowed = {str(option["value"]) for option in field.get("options") or []}
        if value not in allowed:
            raise ValueError(f"{field['label']} 取值不在允许范围内：{value}")


def _sync_environ(updates: dict[str, str], previous: dict[str, str]) -> None:
    """把面板保存实时同步进宿主进程的 ``os.environ``。

    没有这一步的话，面板改动只落 ``var/config.json``，而 OCV 的
    ``gemini_client.language_model()`` 在**调用时刻**读的是环境变量，
    Agent 1 又跑在后端进程内（``pipeline`` 直接 ``import story_agents``）
    —— 于是「面板换了模型，正在跑的后端还在用旧模型」。这也是
    「运行中撞 429 想换模型」的真实出路。

    * 非空值：写入环境（幂等，值相同不写）；
    * 空值（删除）：仅当环境值仍等于面板旧值时摘除，回落到内置默认。
    """
    from . import bootstrap

    try:
        for key, value in updates.items():
            text = str(value or "").strip()
            if text:
                if os.environ.get(key) != text:
                    os.environ[key] = text
            else:
                bootstrap.remove_store_keys_from_environ({key: previous.get(key, "")})
        bootstrap.export_store_to_environ()
    except Exception:  # noqa: BLE001 — 环境同步失败不能挡住保存本身
        pass


def apply(payload: dict[str, Any]) -> dict[str, Any]:
    """写入面板层。空值 = 删除该键（回退到 .env）。"""
    values = payload.get("values")
    if values is not None and not isinstance(values, dict):
        raise ValueError("values 必须是对象")

    by_key = {field["key"]: field for field in FIELDS}
    updates: dict[str, str] = {}
    for key, raw in (values or {}).items():
        name = str(key).strip()
        field = by_key.get(name)
        if field is None:
            continue
        value = _coerce(field, raw)
        _validate(field, value)
        updates[name] = value

    voice_map = payload.get("voice_map")
    if isinstance(voice_map, dict):
        clean_map: dict[str, str] = {}
        valid_targets = {str(item["value"]) for item in MIMO_VOICES}
        valid_targets |= set(config.clone_voices())  # 克隆音色也是合法映射目标
        for source, target in voice_map.items():
            source_name = str(source).strip()
            target_name = str(target or "").strip()
            if not source_name or not target_name:
                continue
            if target_name not in valid_targets:
                raise ValueError(f"音色映射的目标必须是 MiMo 预置音色：{target_name}")
            clean_map[source_name] = target_name
        # 只写用户显式指定的项，其余仍由内置默认表兜底，
        # 这样以后新增 OCV 音色时不必回来补表。
        updates["CLOUD_STACK_VOICE_MAP"] = json.dumps(
            clean_map, ensure_ascii=False, sort_keys=True
        ) if clean_map else ""

    if updates:
        previous = {
            key: str(value or "").strip()
            for key, value in config.store_values().items()
        }
        # 这里是**人在表单上亲手提交**的路径，每个字段已经过 ``_validate``。
        # 因此显式 force=True 绕过 ``update_store`` 的"凭据缩水保护" ——
        # 那条保护针对的是**程序化写入**（测试/探针把真 Key 静默覆盖成占位符，
        # 见 ``config.CredentialClobberError``）。真人操作不该被它挡住。
        config.update_store(updates, force=True)
        _sync_environ(updates, previous)
    return snapshot()


def reset() -> dict[str, Any]:
    """清空面板层，全部回退到 .env / 默认值。"""
    from . import bootstrap

    previous = {
        key: str(value or "").strip()
        for key, value in config.store_values().items()
    }
    config.update_store({}, remove=list(config.store_values()))
    try:
        bootstrap.remove_store_keys_from_environ(previous)
    except Exception:  # noqa: BLE001
        pass
    return snapshot()


# --------------------------------------------------------------------------
# 连通性测试
# --------------------------------------------------------------------------

def _panel_dir() -> Path:
    path = config.plugin_var_dir() / "panel"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_tts(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """合成一小段音频并回传 base64，供面板直接试听。"""
    body = payload or {}
    text = str(body.get("text") or "").strip() or "这是一段配音试听，用来确认音色和接口是否正常。"
    voice = str(body.get("voice") or "").strip()
    if not voice:
        voice = config.mimo_default_voice()

    if not config.mimo_api_key():
        return {"ok": False, "message": "未配置 MiMo API Key"}

    destination = _panel_dir() / "tts_preview.wav"
    started = time.monotonic()
    try:
        mimo_tts.synthesize_to_file(
            text=text,
            destination=destination,
            voice=voice,
            retries=config.mimo_retries(),
        )
    except Exception as exc:  # noqa: BLE001 - 面板要把原因原样告诉用户
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    elapsed = time.monotonic() - started
    blob = destination.read_bytes()
    resolved = mimo_tts.resolve_voice(voice)
    effective_model = (
        "mimo-v2.5-tts-voiceclone"
        if resolved in config.clone_voices()
        else config.mimo_model()
    )
    return {
        "ok": True,
        "message": f"合成成功：{len(blob)} 字节，用时 {elapsed:.1f} 秒",
        "audio": "data:audio/wav;base64," + base64.b64encode(blob).decode("ascii"),
        "detail": {
            "模型": effective_model,
            "请求音色": voice,
            "实际音色": (
                f"克隆音色「{resolved}」（内联参考音频）"
                if resolved in config.clone_voices()
                else resolved
            ),
            "地址": config.mimo_base_url(),
        },
    }


# ---- 克隆音色管理 ----
_CLONE_MIME_EXT = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
}
_CLONE_MAX_BYTES = 8 * 1024 * 1024  # 8MB，10-30 秒参考音频远用不满


def upload_clone_voice(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """接收面板上传的参考音频，登记为克隆音色。

    body: ``{name: 音色名, audio: "data:audio/mpeg;base64,..."}``
    同名覆盖；文件存 ``var/voices/``，注册表存面板层 ``CLOUD_STACK_CLONE_VOICES``。
    """
    body = payload or {}
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 40:
        return {"ok": False, "message": "音色名需要 1-40 个字符"}

    audio = str(body.get("audio") or "").strip()
    if not audio.startswith("data:"):
        return {"ok": False, "message": "音频数据格式不正确（需要 data URI）"}
    header, _, b64data = audio.partition(",")
    mime = header[5:].partition(";")[0].strip().lower()
    ext = _CLONE_MIME_EXT.get(mime)
    if not ext:
        return {
            "ok": False,
            "message": f"不支持的音频格式 {mime or '（未知）'}，请用 mp3 / wav / m4a / aac / ogg / flac",
        }
    try:
        blob = base64.b64decode(b64data, validate=False)
    except (ValueError, TypeError):
        return {"ok": False, "message": "音频 base64 解码失败"}
    if len(blob) < 1024:
        return {"ok": False, "message": "音频内容过短，请上传 10-30 秒的有效录音"}
    if len(blob) > _CLONE_MAX_BYTES:
        return {"ok": False, "message": "音频超过 8MB 上限，请裁剪到 30 秒左右再上传"}

    import hashlib

    stamp = time.strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha1(f"{name}|{stamp}".encode("utf-8")).hexdigest()[:10]
    filename = f"clone_{digest}{ext}"
    (config.clone_voices_dir() / filename).write_bytes(blob)

    registry = config.clone_voices()
    old = registry.get(name)
    registry[name] = {
        "file": filename,
        "mime": mime,
        "bytes": len(blob),
        "uploaded_at": stamp,
    }
    config.update_store(
        {config.CLONE_VOICES_KEY: json.dumps(registry, ensure_ascii=False)}
    )
    # 同名覆盖后清理旧文件，防 var/voices 越攒越多
    if old and old["file"] != filename:
        try:
            (config.clone_voices_dir() / old["file"]).unlink(missing_ok=True)
        except OSError:
            pass
    return {
        "ok": True,
        "message": f"克隆音色「{name}」已上传（{len(blob)} 字节）",
        "clone_voices": clone_voice_rows(),
    }


def delete_clone_voice(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """删除克隆音色：清注册表条目并尝试移除音频文件。"""
    body = payload or {}
    name = str(body.get("name") or "").strip()
    registry = config.clone_voices()
    meta = registry.pop(name, None)
    if meta is None:
        return {"ok": False, "message": f"克隆音色「{name or '（空）'}」不存在"}
    config.update_store(
        {config.CLONE_VOICES_KEY: json.dumps(registry, ensure_ascii=False)}
    )
    try:
        (config.clone_voices_dir() / meta["file"]).unlink(missing_ok=True)
    except OSError:
        pass
    return {
        "ok": True,
        "message": f"克隆音色「{name}」已删除",
        "clone_voices": clone_voice_rows(),
    }


def test_llm() -> dict[str, Any]:
    """连通性测试：按 **当前启用的来源** 打一次，姿势与 OCV 真实调用一致。

    ## ⚠ 必须跟随 `LLM_SOURCE`（F-024 的真实缺陷）

    首版把商汤那一套写死在函数里（`config.sensenova_base_url()` /
    `sensenova_model()` / `sensenova_api_key()`），**完全没读 `LLM_SOURCE`**。

    于是用户在语言模型页选了「云端 OAuth」、保存、点「测试 JSON 输出」，
    实际打的仍然是**商汤** —— detail 里显示的也是商汤的模型名。
    用户会以为「云端 OAuth 没生效」，其实是**这个测试按钮从来没切过路**。

    这个 bug 的隐蔽之处：它只影响**测试按钮**，不影响真实 pipeline
    （真实链路走 `LANGUAGE_PROVIDER`，由 `_sync_language_provider()` 同步）。
    所以「测试通过」与「实际能用」会给出**互相矛盾**的结论 —— 比单纯报错更糟。

    现在两条路都测，且**明确标出在测哪一路**：

    | `LLM_SOURCE` | 打哪里 | 用什么凭据 | 模型 |
    |---|---|---|---|
    | `custom` | 面板填的服务地址 | `SENSENOVA_API_KEY` | `SENSENOVA_MODEL` |
    | `jethub` | 本机桥 `127.0.0.1:<port>/v1` | 桥自己管（账号池） | `JETHUB_MODEL`（`provider::model`） |
    """
    import requests

    if config.llm_source() == "jethub":
        return _test_llm_jethub(requests)
    return _test_llm_custom(requests)


def _test_llm_custom(requests: Any) -> dict[str, Any]:
    """测「自行填写 API」那一路（任意 OpenAI 兼容服务商）。"""
    if not config.sensenova_api_key():
        return {
            "ok": False,
            "message": "未配置语言模型 API Key（当前来源：自行填写 API）",
        }

    endpoint = f"{config.sensenova_base_url()}/chat/completions"
    model = config.sensenova_model()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你只输出 JSON，不要任何解释。"},
            {"role": "user", "content": '返回 {"ok": true, "note": "pong"}'},
        ],
        "max_tokens": 512,
        "response_format": {"type": "json_object"},
    }
    started = time.monotonic()
    try:
        response = requests.post(
            endpoint,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.sensenova_api_key()}",
            },
            json=payload,
            timeout=(15, 180),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    elapsed = time.monotonic() - started
    if response.status_code >= 400:
        return {
            "ok": False,
            "message": f"HTTP {response.status_code}: {response.text[:400]}",
            "detail": {"来源": "自行填写 API", "地址": endpoint, "模型": model},
        }
    return _summarize_llm_response(response, elapsed, "自行填写 API", endpoint, model)


def _test_llm_jethub(requests: Any) -> dict[str, Any]:
    """测「云端 OAuth」那一路 —— 打**本机桥**，不是商汤。

    刻意走 HTTP（而不是直接调 `jethub_bridge.call` 的内部函数）：
    这样测的是 **OCV 将来真正会走的那条网络路径**，包含桥的鉴权与路由。
    只测内部函数会漏掉「桥没起来」「端口不对」这类真实故障。
    """
    from . import jethub_bridge

    model = config.jethub_model()
    if not model:
        return {
            "ok": False,
            "message": (
                "当前来源是「云端 OAuth」，但还没选模型 —— "
                "请在语言模型页的「云端 OAuth 模型」里选一个并保存。"
            ),
            "detail": {"来源": "云端 OAuth"},
        }

    if not jethub_bridge.is_listening():
        error = jethub_bridge.ensure_running()
        if error is not None or not jethub_bridge.is_listening():
            return {
                "ok": False,
                "message": f"云端 OAuth 桥未运行且起不来：{error or '端口无响应'}",
                "detail": {"来源": "云端 OAuth", "模型": model},
            }

    port = jethub_bridge.bridge_port()
    endpoint = f"http://127.0.0.1:{port}/v1/chat/completions"
    payload = {
        # 形如 `loomy::GLM-5.3-Flash`，桥按 `::` 拆分路由到对应提供商
        "model": model,
        "messages": [
            {"role": "system", "content": "你只输出 JSON，不要任何解释。"},
            {"role": "user", "content": '返回 {"ok": true, "note": "pong"}'},
        ],
        # 推理模型会先花掉一部分预算在 reasoning 上（实测短提示 ~48 token），
        # 给 512 以免正文被挤空而误判成失败
        "max_tokens": 512,
    }
    started = time.monotonic()
    try:
        response = requests.post(
            endpoint,
            headers={
                "Content-Type": "application/json",
                # 桥只校验 Host/Origin（回环），不校验 Key；带一个是 OpenAI 客户端习惯
                "Authorization": "Bearer jethub-local",
            },
            json=payload,
            timeout=(15, 180),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "message": f"{type(exc).__name__}: {exc}",
            "detail": {"来源": "云端 OAuth", "地址": endpoint, "模型": model},
        }

    elapsed = time.monotonic() - started
    if response.status_code >= 400:
        return {
            "ok": False,
            "message": f"HTTP {response.status_code}: {response.text[:400]}",
            "detail": {"来源": "云端 OAuth", "地址": endpoint, "模型": model},
        }
    return _summarize_llm_response(response, elapsed, "云端 OAuth", endpoint, model)


def _summarize_llm_response(
    response: Any, elapsed: float, source_label: str, endpoint: str, model: str
) -> dict[str, Any]:
    """把一次聊天补全的响应整理成测试结果（两路共用）。

    `detail` 里**始终标明来源与模型** —— 用户点「测试」就是为了确认
    「到底在用哪一路」，不写清楚等于白测。
    """
    try:
        body = response.json()
        message = (body.get("choices") or [{}])[0].get("message") or {}
        content = str(message.get("content") or "")
        reasoning = str(message.get("reasoning_content") or "")
        usage = body.get("usage") or {}
    except (ValueError, AttributeError, IndexError) as exc:
        return {"ok": False, "message": f"返回体无法解析：{exc}"}

    json_ok = False
    try:
        json.loads(content)
        json_ok = True
    except ValueError:
        json_ok = False

    completion = usage.get("completion_tokens")
    reasoning_tokens = None
    details = usage.get("completion_tokens_details") or {}
    if isinstance(details, dict):
        reasoning_tokens = details.get("reasoning_tokens")

    return {
        "ok": json_ok,
        "message": (
            f"[{source_label}] JSON 约束生效，用时 {elapsed:.1f} 秒"
            if json_ok
            else f"[{source_label}] 返回的不是合法 JSON"
                 f"（OCV 的 Agent 会因此重试）：{content[:120]}"
        ),
        "detail": {
            "来源": source_label,
            "地址": endpoint,
            "模型": model,
            "输出": content[:200],
            "思考内容长度": len(reasoning),
            "补全 token": completion,
            "思考 token": reasoning_tokens,
        },
    }


def test_image(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """经商汤同步接口出一张 1:1 小图。"""
    body = payload or {}
    prompt = str(body.get("prompt") or "").strip() or "一张极简的纯色测试图，中央一个灰色圆点"
    ratio = str(body.get("ratio") or "1:1").strip() or "1:1"
    references: list[str] = []
    reference = str(body.get("reference") or "").strip()
    if reference:
        references = [reference]

    if not config.sensenova_image_api_key():
        return {"ok": False, "message": "未配置图片 API Key（语言模型与图片的 Key 都为空）"}

    started = time.monotonic()
    try:
        blob, size = sense_image.generate(
            prompt=prompt, ratio=ratio, resolution="1k", reference_images=references or None
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    elapsed = time.monotonic() - started
    name = "panel_image_test.jpg"
    sense_image.save_image(blob, _panel_dir() / name)
    return {
        "ok": True,
        "message": f"出图成功：{len(blob)} 字节，尺寸 {size}，用时 {elapsed:.1f} 秒",
        "image": f"{config.shim_base_url()}/panel-files/{name}",
        "detail": {
            "地址": config.sensenova_image_base_url(),
            "模型": config.sensenova_image_edit_model() if references else config.sensenova_image_model(),
            "参考图": "有" if references else "无",
            "水印": "开" if config.image_watermark() else "关",
        },
    }


# ==========================================================================
# Jet Hub：账号管理 / 登录 / 模型开关 / 备份恢复
#
# 全部通过 `jethub_bridge`（本机 Node 桥，默认 :8801）中转。
# **本模块不直接碰凭据** —— 凭据只在 Node 侧读，Python 侧永远拿不到明文，
# 这是刻意的：面板是浏览器可见面，任何能返回凭据明文的接口都是泄漏面。
# ==========================================================================


def jethub_status(payload: dict[str, Any] | None = None, *,
                  provider: str | None = None) -> dict[str, Any]:
    """云端 OAuth 运行态：桥是否在跑 + 已接线的提供商 + 存储落点。

    这是**只读**接口，面板首屏调它。桥没起来时也返回一份可展示的视图
    （`listening: false`），而不是报错 —— 否则面板会显示一片空白。

    `provider` 决定「账号 / 模型 / 积分」这三块查哪个提供商的数据；
    不传则用配置里的默认值。前端切换左栏时会带着新值重新调。

    ## ⚠ 参数顺序为什么必须是 `(payload, *, provider)`（F-009 的真实缺陷）

    `image_shim._panel_call` 的调用约定是 **payload 作第一个位置参数**：

    ```python
    result = handler(payload) if payload is not None else handler()
    ```

    首版签名是 `(provider=None, payload=None)`，于是 HTTP 路径把 **dict 传给了
    `provider`**；而兜底分支只在 `provider is None` 时生效 → 跳过，
    `str(provider)` 得到字面量 `"{'provider': 'buddy'}"`。

    这个垃圾串被发给 `/rpc/accounts.list` 等端点（桥按不存在的 provider 查 →
    恒返回空），又回填到前端的 `jethubProvider` → `capabilities` 按 id 匹配失败
    → **账号列表、模型列表、积分按钮全部空/隐藏**，面板看起来"打不开"。

    **教训**：`_panel_call` 的 handler 一律以 payload 为第一位置参数。
    `provider` 改为**仅关键字参数**，从签名上就杜绝再次错位。
    """
    from . import jethub_bridge

    if provider is None and isinstance(payload, dict):
        provider = payload.get("provider")
    # 防御：万一又有人把 dict 传进 provider（历史调用方），这里兜一下，
    # 不让垃圾串流到下游 —— 这类错误的表现是「一片空白」，极难定位。
    if isinstance(provider, dict):
        provider = provider.get("provider")
    provider = str(provider).strip() if provider is not None else ""

    snapshot = jethub_bridge.status_snapshot()
    target = str(provider or config.jethub_provider() or "buddy")
    result: dict[str, Any] = {
        "ok": True,
        "enabled": config.jethub_enabled(),
        "autostart": config.jethub_autostart(),
        "provider": target,
        "bridge": snapshot,
        "providers": [],
        "capabilities": [],
        "accounts": [],
        "models": [],
        "backups": [],
        "balances": None,
        "storage": {
            "state_dir": str(config.jethub_state_dir()),
            "backup_dir": str(config.jethub_backup_dir()),
        },
    }
    if not snapshot.get("listening"):
        result["message"] = (
            "桥未运行。点击「启动 / 重启桥」，或重启 OCV 后端让它自动拉起。"
        )
        return result

    status = jethub_bridge.call("/rpc/status", {}, timeout=8.0)
    if status["ok"] and isinstance(status["data"], dict):
        value = status["data"].get("value") or {}
        result["providers"] = value.get("providers") or []
        if isinstance(value.get("storage"), dict):
            result["storage"].update(value["storage"])

    caps = jethub_bridge.call("/rpc/credits.capabilities", {}, timeout=8.0)
    if caps["ok"] and isinstance(caps["data"], dict):
        result["capabilities"] = (caps["data"].get("value") or {}).get("capabilities") or []

    accounts = jethub_bridge.call("/rpc/accounts.list", {"provider": target}, timeout=15.0)
    if accounts["ok"] and isinstance(accounts["data"], dict):
        result["accounts"] = (accounts["data"].get("value") or {}).get("accounts") or []
    elif not accounts["ok"]:
        result["accounts_error"] = accounts["error"]

    models = jethub_bridge.call("/rpc/models.list", {"provider": target}, timeout=25.0)
    if models["ok"] and isinstance(models["data"], dict):
        result["models"] = (models["data"].get("value") or {}).get("models") or []
    elif not models["ok"]:
        result["models_error"] = models["error"]

    # 余额：**只在有能力时才请求**（能力门控的正确做法，见 credits.mjs 铁律 2）
    capable = next(
        (row for row in result["capabilities"] if row.get("id") == target),
        None,
    )
    if capable and capable.get("balance"):
        balances = jethub_bridge.call("/rpc/credits.balances", {"provider": target}, timeout=45.0)
        if balances["ok"] and isinstance(balances["data"], dict):
            result["balances"] = (balances["data"].get("value") or {})

    backups = jethub_bridge.call("/rpc/backup.list", {}, timeout=8.0)
    if backups["ok"] and isinstance(backups["data"], dict):
        result["backups"] = (backups["data"].get("value") or {}).get("backups") or []

    return result


def jethub_ensure(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """启动（或确认）桥在跑。面板的「启动 / 重启桥」按钮用。"""
    from . import jethub_bridge

    restart = bool((payload or {}).get("restart"))
    if restart:
        jethub_bridge.stop()
        time.sleep(0.5)
    error = jethub_bridge.ensure_running()
    if error:
        return {"ok": False, "message": error}
    # 拉起后立刻回一份新状态，前端不必再打一次
    return jethub_status()


def jethub_stop(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """停掉由本进程拉起的桥。"""
    from . import jethub_bridge

    stopped = jethub_bridge.stop()
    return {
        "ok": True,
        "message": "桥已停止" if stopped else "没有由本进程拉起的桥（可能由别的进程托管）",
    }


def jethub_login_start(payload: dict[str, Any]) -> dict[str, Any]:
    """开始两步式登录：返回 loginUrl 供前端**立刻** window.open。

    为什么必须两步：浏览器只在用户点击后的短暂窗口（约 5 秒）内允许
    `window.open`。等整个登录走完再返回 URL，手势早已过期，弹窗会被拦截。
    """
    from . import jethub_bridge

    provider = str((payload or {}).get("provider") or config.jethub_provider())
    nickname = str((payload or {}).get("nickname") or "")

    error = jethub_bridge.ensure_running()
    if error:
        return {"ok": False, "message": f"桥未就绪：{error}"}

    result = jethub_bridge.call(
        "/rpc/accounts.login.start",
        {"provider": provider, "nickname": nickname},
        timeout=45.0,
    )
    if not result["ok"]:
        return {"ok": False, "message": result["error"] or "登录启动失败"}
    value = (result["data"] or {}).get("value") or {}
    return {
        "ok": True,
        "login_url": value.get("loginUrl", ""),
        "account_id": value.get("accountId", ""),
        "provider": provider,
        "message": "已打开登录窗口，请在浏览器里完成授权。",
    }


def jethub_login_poll(payload: dict[str, Any]) -> dict[str, Any]:
    """轮询登录是否完成。"""
    from . import jethub_bridge

    provider = str((payload or {}).get("provider") or config.jethub_provider())
    account_id = str((payload or {}).get("account_id") or "")
    if not account_id:
        return {"ok": False, "message": "缺少 account_id"}

    result = jethub_bridge.call(
        "/rpc/accounts.poll",
        {"provider": provider, "accountId": account_id},
        timeout=20.0,
    )
    if not result["ok"]:
        return {"ok": False, "message": result["error"] or "轮询失败"}
    value = (result["data"] or {}).get("value") or {}
    return {
        "ok": True,
        "done": bool(value.get("done")),
        "success": bool(value.get("success")),
        "pending": bool(value.get("pending")),
        "account": value.get("account"),
        "message": value.get("error") or ("登录完成" if value.get("done") else "等待授权中…"),
    }


def jethub_account_action(payload: dict[str, Any]) -> dict[str, Any]:
    """账号操作：`toggle`（启用/停用）、`remove`（删除）。

    ⚠ **面板上已没有 `refresh`（手动刷新凭据）** —— 用户要求去掉该按钮。

    凭据刷新是**后台自动**的（与参考项目一致）：桥在每次调用前走
    `resolveUsableCredential()`，发现凭据过期或即将过期（留 60 秒余量）
    就按**账号自己的** credentialRef 主动刷新再发请求。

    `refresh` 分支**保留**：它是桥的既有 RPC（`accounts.refresh`），
    去掉会让「排障时想手动触发一次刷新」失去手段；只是面板不再暴露按钮。
    """
    from . import jethub_bridge

    action = str((payload or {}).get("action") or "")
    provider = str((payload or {}).get("provider") or config.jethub_provider())
    account_id = str((payload or {}).get("account_id") or "")
    if action not in {"toggle", "remove", "refresh"}:
        return {"ok": False, "message": f"未知操作：{action}"}
    if not account_id:
        return {"ok": False, "message": "缺少 account_id"}

    if action == "toggle":
        body = {
            "provider": provider,
            "accountId": account_id,
            "enabled": bool((payload or {}).get("enabled")),
        }
        path = "/rpc/accounts.toggle"
    elif action == "remove":
        body = {"provider": provider, "accountId": account_id}
        path = "/rpc/accounts.remove"
    else:
        body = {"provider": provider, "accountId": account_id}
        path = "/rpc/accounts.refresh"

    result = jethub_bridge.call(path, body, timeout=60.0)
    if not result["ok"]:
        return {"ok": False, "message": result["error"] or f"{action} 失败"}
    value = (result["data"] or {}).get("value") or {}
    return {"ok": True, "accounts": value.get("accounts") or [], "message": "已更新"}


def jethub_model_toggle(payload: dict[str, Any]) -> dict[str, Any]:
    """切换某模型是否出现在可选列表里（黑名单制）。

    `model_id` 传 `__ALL__` 时走**批量**分支 —— 前端「打开全部 / 关闭全部」
    用。之所以复用同一个端点而不是另开一个，是为了让前端只记一个 URL；
    桥侧的 `models.setAllDisabled` 有**不对称语义**（见那里的注释）。
    """
    from . import jethub_bridge

    provider = str((payload or {}).get("provider") or config.jethub_provider())
    model_id = str((payload or {}).get("model_id") or "")
    if not model_id:
        return {"ok": False, "message": "缺少 model_id"}

    if model_id == "__ALL__":
        result = jethub_bridge.call(
            "/rpc/models.setAllDisabled",
            {"provider": provider, "disabled": bool((payload or {}).get("disabled"))},
            timeout=90.0,
        )
    else:
        result = jethub_bridge.call(
            "/rpc/models.setDisabled",
            {"provider": provider, "modelId": model_id,
             "disabled": bool((payload or {}).get("disabled"))},
            timeout=30.0,
        )
    if not result["ok"]:
        return {"ok": False, "message": result["error"] or "切换失败"}
    value = (result["data"] or {}).get("value") or {}
    return {"ok": True, "models": value.get("models") or [], "message": "已更新"}


def jethub_models_all(payload: dict[str, Any]) -> dict[str, Any]:
    """批量开关模型（「打开全部 / 关闭全部」的专用端点）。

    前端 `cloud-oauth.js` 调的是 `/api/panel/jethub/models/all` ——
    首版**只注册了 `jethub/model/toggle`，漏了这个路由**，于是两个按钮
    必然命中 404 兜底（用户只看到「失败」）。桥侧的
    `models.setAllDisabled` 因此成了**死代码**。
    """
    from . import jethub_bridge

    provider = str((payload or {}).get("provider") or config.jethub_provider())
    disabled = bool((payload or {}).get("disabled"))
    result = jethub_bridge.call(
        "/rpc/models.setAllDisabled",
        {"provider": provider, "disabled": disabled},
        timeout=120.0,
    )
    if not result["ok"]:
        return {"ok": False, "message": result["error"] or "批量切换失败"}
    value = (result["data"] or {}).get("value") or {}
    models = value.get("models") or []
    hidden = len([m for m in models if m.get("disabled")])
    return {
        "ok": True,
        "models": models,
        "message": (
            f"{'关闭' if disabled else '打开'}全部完成："
            f"共 {len(models)} 个模型，当前隐藏 {hidden} 个。"
        ),
    }


def jethub_test(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """真实对话测试：拿当前配置的某个模型发一次最小请求。

    这是**唯一会真的花额度**的面板按钮，但它是验收「账号可用」的唯一可靠手段
    —— 凭据存在不代表上游认它（可能已过期或被风控）。
    """
    from . import jethub_bridge

    provider = str((payload or {}).get("provider") or config.jethub_provider())
    model = str((payload or {}).get("model") or "").strip()
    if not model:
        configured = config.jethub_model()
        if configured:
            model = configured
        else:
            models = jethub_bridge.call("/rpc/models.list", {"provider": provider}, timeout=25.0)
            rows = []
            if models["ok"] and isinstance(models["data"], dict):
                rows = ((models["data"].get("value") or {}).get("models")) or []
            usable = [row for row in rows if not row.get("disabled")]
            if not usable:
                return {
                    "ok": False,
                    "message": "没有可用模型：请先登录账号，再点「刷新模型」拉取清单。",
                }
            model = str(usable[0].get("id") or "")

    started = time.monotonic()
    # ⚠ 必须带 `?provider=`：桥的 `complete()` 会按 `provider::model` 拆分路由，
    # 但**手工填的裸 model id**（没有前缀）会用查询参数兜底。首版不带它，
    # 于是「在面板里选了 X 家、手填一个模型」会被强行发往 buddy ——
    # 与界面显示不符，且报错信息也指错方向。
    url = f"/v1/chat/completions?provider={quote(provider, safe='')}"
    result = jethub_bridge.call(
        url,
        {
            "model": model,
            "messages": [
                {"role": "user", "content": "只回复两个字：可用"},
            ],
            # 给足预算：推理模型会先花掉一部分在 reasoning 上（实测一个短提示
            # 就用掉 ~48 个 reasoning token），预算太小会 finish_reason=length
            # 且正文为空 —— 那是**预期**行为，不是缺陷，但会让测试显示失败。
            "max_tokens": 1024,
            "temperature": 0,
        },
        timeout=120.0,
    )
    elapsed = time.monotonic() - started

    if not result["ok"]:
        return {
            "ok": False,
            "message": f"调用失败（{elapsed:.1f} 秒）：{result['error']}",
            "detail": {"provider": provider, "模型": model, "HTTP": result["status"]},
        }
    data = result["data"] or {}
    choices = data.get("choices") or [{}]
    content = str((choices[0].get("message") or {}).get("content") or "")
    return {
        "ok": bool(content.strip()),
        "message": f"调用成功（{elapsed:.1f} 秒）：{content.strip()[:80]}",
        "detail": {
            "provider": provider,
            "模型": data.get("model") or model,
            "用量": data.get("usage") or {},
            "finish_reason": choices[0].get("finish_reason"),
        },
    }


def jethub_backup_action(payload: dict[str, Any]) -> dict[str, Any]:
    """备份 / 恢复。

    | action | 说明 |
    |---|---|
    | `export` | **回文件本体**（前端用 Blob + `<a download>` 让用户自己挑存哪）；`save_server=true` 时另存一份到插件目录 |
    | `import` | 从**用户上传的文件**恢复（`document` 传内容）；`name` 是服务端留档的向后兼容路径 |
    | `delete` | 删除服务端留档的备份 |

    **用户明确要求「从本地上传文件恢复，而不是固定放到某文件夹」** ——
    所以 `import` 的主路径是前端读文件后把**内容**发来，服务端不再要求
    用户手工往 `state/backups/` 里塞文件。

    备份**含凭据明文**，所以：绝不写日志、绝不回显到界面（只在下载时给浏览器）。
    """
    from . import jethub_bridge

    action = str((payload or {}).get("action") or "export")

    if action == "export":
        save_server = bool((payload or {}).get("save_server"))
        result = jethub_bridge.call(
            "/rpc/backup.export",
            {
                "name": (payload or {}).get("name") or "",
                # download=False 才会落服务端；默认回文件给浏览器下载
                "download": not save_server,
            },
            timeout=30.0,
        )
        if not result["ok"]:
            return {"ok": False, "message": result["error"] or "导出失败"}
        value = (result["data"] or {}).get("value") or {}
        summary = (
            f"{value.get('accounts', 0)} 个账号、{value.get('credentials', 0)} 份凭据"
        )
        out: dict[str, Any] = {
            "ok": True,
            "name": value.get("name"),
            "mode": value.get("mode"),
        }
        if value.get("mode") == "download":
            out["document"] = value.get("document")
            out["message"] = f"备份已生成（{summary}），请保存到本地。**文件含凭据明文，请自行保管。**"
        else:
            out["path"] = value.get("path")
            out["message"] = f"已在插件目录留档一份备份（{summary}）。"
        return out

    if action == "import":
        document = (payload or {}).get("document")
        name = str((payload or {}).get("name") or "")
        if document is None and not name:
            return {"ok": False, "message": "请先选择要恢复的备份文件"}

        body: dict[str, Any] = {"mode": str((payload or {}).get("mode") or "merge")}
        if document is not None:
            body["document"] = document
            body["file_name"] = str((payload or {}).get("file_name") or "")
        else:
            body["name"] = name

        result = jethub_bridge.call("/rpc/backup.import", body, timeout=90.0)
        if not result["ok"]:
            return {"ok": False, "message": result["error"] or "恢复失败"}
        value = (result["data"] or {}).get("value") or {}
        # 提示里说清「来源格式」，因为它决定了用户下次该从哪导出
        source_label = "DSH 导出的备份" if value.get("source") == "dsh" else "本插件导出的备份"
        parts = [
            f"已从「{value.get('name')}」恢复（识别为{source_label}，模式 {value.get('mode')}）：",
            f"{value.get('accountCount', 0)} 个账号、{value.get('credentialsCount', 0)} 份凭据。",
        ]
        missing = int(value.get("missingCredentials") or 0)
        expired = int(value.get("expiredAccounts") or 0)
        if missing:
            parts.append(f"⚠ 其中 {missing} 个账号**没有对应凭据**（不会出现在可用列表里）。")
        if expired:
            parts.append(f"⚠ 其中 {expired} 个账号的凭据**已过期**，需要重新登录。")
        if value.get("mode") == "replace":
            parts.append("请点「刷新状态」查看。")
        return {"ok": True, "message": "".join(parts)}

    if action == "delete":
        name = str((payload or {}).get("name") or "")
        if not name:
            return {"ok": False, "message": "请选择要删除的备份"}
        result = jethub_bridge.call("/rpc/backup.remove", {"name": name}, timeout=15.0)
        if not result["ok"]:
            return {"ok": False, "message": result["error"] or "删除失败"}
        return {"ok": True, "message": f"已删除备份 {name}"}

    return {"ok": False, "message": f"未知操作：{action}"}


# --------------------------------------------------------------------------
# 积分 / 签到
# --------------------------------------------------------------------------


def jethub_credits(payload: dict[str, Any]) -> dict[str, Any]:
    """积分查询与「一键签到」。

    | action | 说明 |
    |---|---|
    | `balances` | 查某提供商的余额（逐账号） |
    | `claimAll` | **一键签到**：跨提供商串行，绝不并发 |
    | `claimOnboarding` | 领新手任务（目前只有 Loomy） |

    ⚠ **签到是真实写请求**。原 Jet Hub 的实现注释明确：跨渠道并发会
    「同时发出多路真实的领积分写请求」从而触发风控，所以桥侧用
    `for ... await` 串行 —— 这里不做任何并发优化。
    """
    from . import jethub_bridge

    action = str((payload or {}).get("action") or "balances")
    if action == "balances":
        provider = str((payload or {}).get("provider") or config.jethub_provider())
        result = jethub_bridge.call("/rpc/credits.balances", {"provider": provider}, timeout=60.0)
        if not result["ok"]:
            return {"ok": False, "message": result["error"] or "查询余额失败"}
        return {"ok": True, "data": (result["data"] or {}).get("value") or {}}

    if action == "claimAll":
        providers = (payload or {}).get("providers")
        body = {"providers": providers} if isinstance(providers, list) and providers else {}
        # 串行签到可能耗时较久（多提供商 × 多账号 × 真实网络往返）
        result = jethub_bridge.call("/rpc/credits.claimAll", body, timeout=300.0)
        if not result["ok"]:
            return {"ok": False, "message": result["error"] or "签到失败"}
        value = (result["data"] or {}).get("value") or {}
        return {
            "ok": True,
            "message": str(value.get("summary") or "签到完成"),
            "results": value.get("results") or [],
            "total_claimed": value.get("totalClaimed", 0),
            # 「今天已领过」是**正常状态**，不是失败 —— 单列出来，
            # 否则用户会看到「失败 N 个」的假告警（见 docs/JETHUB.md F-006）。
            "total_already_claimed": value.get("totalAlreadyClaimed", 0),
            "total_failed": value.get("totalFailed", 0),
            "total_skipped": value.get("totalSkipped", 0),
        }

    if action == "claimOnboarding":
        provider = str((payload or {}).get("provider") or "loomy")
        result = jethub_bridge.call(
            "/rpc/credits.claimOnboarding", {"provider": provider}, timeout=180.0
        )
        if not result["ok"]:
            return {"ok": False, "message": result["error"] or "领取失败"}
        value = (result["data"] or {}).get("value") or {}
        return {
            "ok": True,
            "message": f"已为 {value.get('claimed', 0)} 个账号领取新手任务",
            "data": value,
        }

    return {"ok": False, "message": f"未知操作：{action}"}
