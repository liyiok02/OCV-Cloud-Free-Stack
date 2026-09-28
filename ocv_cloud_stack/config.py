"""插件配置读取。

键名统一带 ``CLOUD_STACK_`` / ``MIMO_`` / ``SENSENOVA_`` 前缀。

读取优先级（从高到低）：

1. ``var/config.json`` —— 配置面板写入的那一层。**故意放在最高优先级**，
   因为它才是"当前生效值"的唯一真相：面板改完立即生效，不用重启 OCV。
2. ``os.environ`` —— OCV 自己的 ``load_project_env()`` 会把 .env 灌进来。
   注意这层是**进程启动时的快照**，运行期改 .env 不会更新它。
3. ``.env`` 文件 —— install 时写入的基线。
4. 内置默认值。

把面板层放在最上面，是为了让"改完就能用"成立：否则 .env 的值会经
``os.environ`` 反过来盖住面板的修改。
"""

from __future__ import annotations

import base64
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable

_ENV_CACHE: dict[str, str] | None = None
_ENV_LOCK = threading.Lock()

_STORE_CACHE: tuple[float, int, dict[str, str]] | None = None
_STORE_LOCK = threading.Lock()
_STORE_FILE_NAME = "config.json"

# OCV 前端音色名 -> MiMo 预置音色。没有这张表的话，前端选"田叔·沙哑烟嗓男声"
# 会落到默认女声上。用户可以逐项覆盖，覆盖结果写进 var/config.json。
DEFAULT_VOICE_MAP: dict[str, str] = {
    # 男声
    "Eldric Sage": "苏打",
    "Vincent": "苏打",
    "Neil": "苏打",
    "Arthur": "苏打",
    "Ethan": "苏打",
    "Moon": "苏打",
    "Kai": "苏打",
    "Nofish": "苏打",
    "Mochi": "苏打",
    "Pip": "苏打",
    "Ryan": "苏打",
    "Aiden": "苏打",
    "Bodega": "苏打",
    "Alek": "苏打",
    # 女声
    "Elias": "冰糖",
    "Seren": "冰糖",
    "Maia": "冰糖",
    "Serena": "冰糖",
    "Cherry": "冰糖",
    "Chelsie": "冰糖",
    "Momo": "茉莉",
    "Vivian": "茉莉",
    "Bella": "冰糖",
    "Mia": "茉莉",
    "Bellona": "冰糖",
    "Bunny": "冰糖",
    "Nini": "冰糖",
    "Stella": "冰糖",
    "Jennifer": "冰糖",
    "Katerina": "冰糖",
    "Sonrisa": "茉莉",
    "Dolce": "苏打",
}


def plugin_root() -> Path:
    """``<OCV 项目根>/plugins/cloud_free_stack``"""
    return Path(__file__).resolve().parent.parent


def project_root() -> Path:
    """OCV 项目根目录。"""
    configured = os.getenv("OCV_PROJECT_ROOT", "").strip()
    if configured and Path(configured).is_dir():
        return Path(configured).resolve()
    # plugins/cloud_free_stack/ocv_cloud_stack/config.py -> 上溯四级
    return Path(__file__).resolve().parents[3]


def plugin_var_dir() -> Path:
    """插件的运行期目录（shim 截图、缓存、pid 等）。"""
    path = plugin_root() / "var"
    path.mkdir(parents=True, exist_ok=True)
    return path


def store_path() -> Path:
    """配置面板写入的文件。"""
    return plugin_var_dir() / _STORE_FILE_NAME


# --------------------------------------------------------------------------
# 配置面板层（var/config.json）
# --------------------------------------------------------------------------

def _read_store() -> dict[str, str]:
    """读取面板配置。按 (mtime, size) 判断缓存是否可用。

    必须按 mtime 失效而不是永久缓存：module1 的子进程要连续合成几十句配音，
    用户完全可能在合成中途去面板改音色，这时下一句就应该用新值。
    """
    global _STORE_CACHE
    path = store_path()
    try:
        stat = path.stat()
        signature = (stat.st_mtime, stat.st_size)
    except OSError:
        with _STORE_LOCK:
            _STORE_CACHE = None
        return {}

    with _STORE_LOCK:
        if _STORE_CACHE is not None and _STORE_CACHE[:2] == signature:
            return _STORE_CACHE[2]
        values: dict[str, str] = {}
        try:
            parsed: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                if value is None:
                    continue
                values[str(key).strip()] = str(value)
        _STORE_CACHE = (signature[0], signature[1], values)
        return values


def store_values() -> dict[str, str]:
    """当前面板层的全部键值（只读副本）。"""
    return dict(_read_store())


# 凭据类键名（含这些片段的键，其值被视为密文，受下面的"缩水保护"约束）
_SECRET_HINTS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "ACCESS_KEY")

# 一个被当成密文的旧值至少要这么长，才有资格触发保护
_SECRET_MIN_LEN = 20

# 新值短于旧值的这个比例时，视为"疑似把真 Key 覆盖成了占位符"
_SECRET_SHRINK_RATIO = 0.34


class CredentialClobberError(RuntimeError):
    """拒绝用一个明显是占位符的短值覆盖已有的长凭据。

    这是**事故驱动**的保护。2026-09-24 我（开发方）在一条诊断探针里写了
    ``update_store({'AGNES_API_KEY': 'k'}, remove=[...])``，而这个函数的语义是
    **先 remove 再写入**，于是用户 51 字符的真 Key 被静默替换成 ``'k'``，
    且没有任何报错 —— 直到后面核查才发现。同类事故在 F-003 已经发生过一次
    （整体还原 ``.env`` 备份，反而弄丢了真实凭据）。

    ``update_store`` 同时被**面板保存**（用户真实修改）和**测试/探针**调用，
    而"把真 Key 换成 1 个字符"在两种场景下都几乎不可能是本意：
    要清空应当显式传空串，要换 Key 应当给另一个完整 Key。
    所以这里宁可**报错拒写**，也不做静默破坏。

    确需写入超短值时，显式传 ``force=True``（测试与"清空"路径都走这条）。
    """


def _looks_secret(key: str) -> bool:
    name = str(key or "").upper()
    return any(hint in name for hint in _SECRET_HINTS)


def update_store(updates: dict[str, str], remove: Iterable[str] = (),
                 *, force: bool = False) -> dict[str, str]:
    """合并写入面板层。空字符串等价于删除该键（回退到 .env）。

    注意：合并计算必须在加锁之前完成 —— ``_read_store()`` 内部也要拿同一把锁，
    在锁里调用它会自锁死。

    ## 凭据缩水保护（``force=False`` 时的默认行为）

    若某个键名像凭据（``*_API_KEY`` / ``*_TOKEN`` …），且**旧值很长**、
    **新值显著更短**，则抛 :class:`CredentialClobberError` 并**拒绝写入**。
    详见该异常类的文档 —— 这条保护来自一次真实事故。

    要合法地清空凭据：传空串（走删除分支，不受保护限制）。
    要合法地写入超短值：显式 ``force=True``。
    """
    global _STORE_CACHE
    target = store_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    values = dict(_read_store())

    if not force:
        for key, value in updates.items():
            name = str(key).strip()
            if not name or not _looks_secret(name):
                continue
            old = str(values.get(name) or "").strip()
            new = str(value or "").strip()
            if not new:
                continue  # 空值 = 删除，正常处理
            if len(old) >= _SECRET_MIN_LEN and len(new) < len(old) * _SECRET_SHRINK_RATIO:
                raise CredentialClobberError(
                    f"拒绝把 {name} 从 {len(old)} 字符覆盖成 {len(new)} 字符"
                    f"（疑似用占位符覆盖真实凭据）。"
                    f"如确需如此请显式传 force=True；清空请传空字符串。"
                )

    for key in remove:
        values.pop(str(key), None)
    for key, value in updates.items():
        name = str(key).strip()
        text = str(value or "").strip()
        if not name:
            continue
        if text:
            values[name] = text
        else:
            values.pop(name, None)

    payload = json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True)
    with _STORE_LOCK:
        temporary = target.with_name(target.name + ".part")
        temporary.write_text(payload + "\n", encoding="utf-8")
        os.replace(temporary, target)
        _STORE_CACHE = None
    return values


def reload_store() -> None:
    """丢弃面板层缓存。"""
    global _STORE_CACHE
    with _STORE_LOCK:
        _STORE_CACHE = None


def layer_of(key: str) -> str:
    """这个键当前的值来自哪一层？面板用它给用户标注。"""
    value = _read_store().get(key)
    if value is not None and value.strip():
        return "panel"
    raw = os.getenv(key)
    if raw is not None and raw.strip():
        return "environ"
    value = _read_env_file().get(key)
    if value is not None and value.strip():
        return "env"
    return "default"


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_env_file() -> dict[str, str]:
    global _ENV_CACHE
    with _ENV_LOCK:
        if _ENV_CACHE is not None:
            return _ENV_CACHE
        values: dict[str, str] = {}
        env_path = project_root() / ".env"
        if env_path.is_file():
            try:
                text = env_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = _strip_quotes(value)
        _ENV_CACHE = values
        return values


def reload_env() -> None:
    """丢弃 .env 缓存（安装脚本改完文件后调用）。"""
    global _ENV_CACHE
    with _ENV_LOCK:
        _ENV_CACHE = None


def get(key: str, default: str = "") -> str:
    from_panel = _read_store().get(key)
    if from_panel is not None and from_panel.strip():
        return from_panel.strip()
    value = os.getenv(key)
    if value is not None and value.strip():
        return value.strip()
    from_file = _read_env_file().get(key)
    if from_file is not None and from_file.strip():
        return from_file.strip()
    return default


def get_bool(key: str, default: bool = False) -> bool:
    raw = get(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def get_int(key: str, default: int) -> int:
    raw = get(key, "").strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float) -> float:
    raw = get(key, "").strip()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# 具体配置项
# --------------------------------------------------------------------------

def enabled() -> bool:
    """总开关。关掉之后本插件对 OCV 完全透明。"""
    return get_bool("CLOUD_STACK_ENABLED", True)


def debug() -> bool:
    return get_bool("CLOUD_STACK_DEBUG", False)


# ---- MiMo TTS ----
def mimo_api_key() -> str:
    return get("MIMO_API_KEY")


def mimo_base_url() -> str:
    return get("MIMO_TTS_BASE_URL", "https://api.xiaomimimo.com/v1").rstrip("/")


def mimo_model() -> str:
    return get("MIMO_TTS_MODEL", "mimo-v2.5-tts")


def mimo_default_voice() -> str:
    return get("MIMO_TTS_VOICE", "冰糖")


def mimo_timeout() -> tuple[float, float]:
    return (
        get_float("MIMO_TTS_CONNECT_TIMEOUT", 20.0),
        get_float("MIMO_TTS_READ_TIMEOUT", 180.0),
    )


def mimo_retries() -> int:
    return max(1, get_int("MIMO_TTS_RETRIES", 3))


def mimo_engine_enabled() -> bool:
    """MiMo 是否作为**独立第 4 个配音引擎**（与 Qwen-TTS 并列）。

    ``1``（默认）= 前端下拉同时出现「MiMo TTS（云端免费栈）」与原生「Qwen-TTS」，
    两者互不覆盖；``0`` = 退回 1.9.x 的旧行为（MiMo 顶替 Qwen 槽位）。

    登记见 DEVLOG §7 与版本 1.10.0。
    """
    return get_bool("CLOUD_STACK_MIMO_SEPARATE_ENGINE", True)


def voice_map() -> dict[str, str]:
    """把 OCV 前端写死的 Qwen 音色名映射到 MiMo 音色名。

    先套内置默认表（保证开箱就有合理的男女声区分），再叠加用户覆盖。
    面板写入的是**完整映射表**，因此用户改过的项会稳定生效。
    """
    merged = dict(DEFAULT_VOICE_MAP)
    merged.update(_parse_voice_map(get("CLOUD_STACK_VOICE_MAP", "")))
    return merged


def _parse_voice_map(raw: str) -> dict[str, str]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


# ---- 克隆音色（mimo-v2.5-tts-voiceclone）----
# 注册表存面板层 CLOUD_STACK_CLONE_VOICES：{名称: {file, mime, bytes, uploaded_at}}
# 参考音频本体存 var/voices/<随机名>.<ext>，每次合成请求内联为 data URI。
CLONE_VOICES_KEY = "CLOUD_STACK_CLONE_VOICES"


def clone_voices_dir() -> Path:
    """克隆参考音频的存放目录（var/voices/）。"""
    path = plugin_var_dir() / "voices"
    path.mkdir(parents=True, exist_ok=True)
    return path


def clone_voices() -> dict[str, dict[str, Any]]:
    """解析克隆音色注册表。坏条目静默跳过，不让一个坏值毁掉整个面板。"""
    text = str(get(CLONE_VOICES_KEY, "") or "").strip()
    if not text:
        return {}
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for name, meta in parsed.items():
        clean_name = str(name).strip()
        if not clean_name or not isinstance(meta, dict):
            continue
        result[clean_name] = {
            "file": str(meta.get("file") or ""),
            "mime": str(meta.get("mime") or "audio/mpeg"),
            "bytes": int(meta.get("bytes") or 0),
            "uploaded_at": str(meta.get("uploaded_at") or ""),
        }
    return result


def clone_audio_data_uri(name: str) -> str | None:
    """返回克隆音色的参考音频 data URI；音色不存在或文件丢失返回 None。"""
    meta = clone_voices().get(str(name).strip())
    if not meta:
        return None
    path = clone_voices_dir() / meta["file"]
    if not path.is_file():
        return None
    blob = path.read_bytes()
    return f"data:{meta['mime']};base64,{base64.b64encode(blob).decode('ascii')}"


# ---- 商汤 LLM ----
def sensenova_api_key() -> str:
    return get("SENSENOVA_API_KEY")


def sensenova_base_url() -> str:
    return get("SENSENOVA_API_BASE", "https://token.sensenova.cn/v1").rstrip("/")


def sensenova_model() -> str:
    return get("SENSENOVA_MODEL", "deepseek-v4-pro")


def sensenova_max_tokens() -> int:
    """输出 token 上限。**0 = 不限制，交给模型自己的默认值**（默认）。

    ## 为什么默认改成"不限制"（F-017）

    原先这里默认 16384，起因是"推理模型的思考 token 会挤占输出预算"。
    但实测发现它会造成**比它想解决的问题更严重的故障**：

    * ``director_prompt_editor.finalize_prompts`` 要一次性输出全部海报的提示词，
      数量随文案长度线性增长（实测 21 张≈9.4k 字符、39 张≈18k 字符、
      54 张≈24k+ 字符）。硬定 16384 时，长文案**必然**撞满上限被截断：
      ``GeminiOutputTruncated: completion_tokens=16384, reasoning_tokens=0``。
      这不是限流、不是 Key、不是重试不够，是**输出预算被我们定小了**。

    * 服务商 ``/models`` 声明的 ``max_output_length`` **不可信**（实测
      ``deepseek-flash`` 声明 65536，真实上限 **393216**，少报 6 倍），
      且每个模型都不同（``glm-5.2`` 131072、``kimi-k3`` 1048576…）。
      端点**没有**任何字段能给出真值 ⇒ "按模型自动取值"只能靠猜，
      而**省略 ``max_tokens`` 让模型用自己的默认值**才是真正的"按模型自动"。

    实测（2026-09-25）：省略 ``max_tokens`` 时上游默认给 **65536**
    （是原硬定值的 4 倍），且各模型各按其自身默认执行。

    ## 这个函数返回 0 时的行为

    ``patches`` 会**从请求体里删掉** ``max_tokens`` 键 —— 不能只是"不传"，
    因为 OCV 源码里是 ``max_output_tokens or int(os.getenv("GEMINI_MAX_TOKENS", "4096"))``，
    **不传反而会落到 4096（比 16384 更糟）**。所以必须在请求体层删除。

    用户显式填了正整数时，仍按"抬下限"语义生效（见 ``patches``），
    保留给需要精确控制成本/时长的场景。
    """
    return max(0, _migrated_max_tokens())


# 旧版硬编码的默认值。`install` 会把它写进 `.env`，而 `_render_block` 的
# "已有非空值就沿用"会让它**永远留在那里** —— 只改代码默认值不生效。
# 因此这里把"恰好等于旧默认值"视为"用户没设过"，按新默认（0=不限制）处理。
#
# 为什么敢这么判：16384 是**插件自己**注入的出厂值，不是用户直觉会填的数；
# 而它正是造成 F-017 截断故障的那个值。用户若真想要 16384，重新填一次即可
# （填了就写进面板层，本迁移只作用于"从没人动过"的情形）。
_LEGACY_MAX_TOKENS_DEFAULT = 16384


def _migrated_max_tokens() -> int:
    """解析 ``SENSENOVA_MAX_TOKENS``，把**旧版出厂默认值**迁移成"不限制"。

    ## 迁移只作用于"插件自己写进 .env 的基线层"

    旧版 `install` 往 `.env` 写了 `SENSENOVA_MAX_TOKENS=16384`，而
    `_render_block()` 的策略是"已有非空值就沿用"（那是为了不覆盖用户填的 Key）
    ⇒ 旧值会**永久留在 `.env`**，只改代码默认值根本不生效。

    ## 但**面板层是用户显式意志，绝不能被迁移吃掉**

    ⚠ 第一版写成"值等于 16384 就当成没设过"，结果：用户在面板里**明确填 16384**
    时也被静默改成 0 —— 用户的显式设置被我们的迁移逻辑覆盖，属于
    **静默违背用户意图**（本仓库已有多次同类教训，见 F-003 / F-012）。
    所以这里按**层次**判断，而不是按"值等于多少"：

    * 面板层有非空值 ⇒ **一律以用户为准**，原样返回；
    * 否则落到 ``.env`` / 环境变量：只有"恰好等于旧出厂默认值"才迁移成 0，
      因为那个值只可能是插件自己写进去的。
    """
    from_panel = str(_read_store().get("SENSENOVA_MAX_TOKENS") or "").strip()
    if from_panel:
        try:
            return max(0, int(from_panel))
        except (TypeError, ValueError):
            return 0

    text = str(os.getenv("SENSENOVA_MAX_TOKENS") or "").strip()
    if not text:
        text = str(_read_env_file().get("SENSENOVA_MAX_TOKENS") or "").strip()
    if not text:
        return 0
    try:
        value = int(text)
    except (TypeError, ValueError):
        return 0
    if value == _LEGACY_MAX_TOKENS_DEFAULT:
        # 只可能是旧版 install 写进 .env 的出厂值 ⇒ 迁移成"不限制"
        return 0
    return max(0, value)


# ---- 语言模型重试（30 档阶梯退避，与视频共用档位表）----
def llm_retry_enabled() -> bool:
    """语言模型请求是否启用 30 档阶梯重试。默认**开启**。

    档位与云端视频**完全一致**（共用 :data:`LADDER_RETRY_TIERS`），
    只对上游"明确可重试"的错误（429 限流 / 5xx / 超时）起作用；
    鉴权与参数错误照旧立刻失败，所以开启它不会让真正需要排查的错误被掩盖。

    关闭后完全回到 OCV 原生行为（``GEMINI_RETRY_COUNT`` 默认 3 次）。
    """
    return get_bool("CLOUD_STACK_LLM_RETRY_ENABLED", True)


def llm_retry_count() -> int:
    """最多重试多少次（不含首次请求），默认 30 —— 与视频侧一致，共用档位表。"""
    return max(0, min(60, get_int("CLOUD_STACK_LLM_RETRY_COUNT", 30)))


# ---- 商汤图片 ----
def sensenova_image_api_key() -> str:
    return get("SENSENOVA_IMAGE_API_KEY") or sensenova_api_key()


def sensenova_image_base_url() -> str:
    return get("SENSENOVA_IMAGE_API_BASE", "").rstrip("/") or sensenova_base_url()


def sensenova_image_model() -> str:
    return get("SENSENOVA_IMAGE_MODEL", "sensenova-u1.5-lite")


def sensenova_image_edit_model() -> str:
    return get("SENSENOVA_IMAGE_EDIT_MODEL", "") or sensenova_image_model()


def shim_port() -> int:
    return get_int("CLOUD_STACK_SHIM_PORT", 8799)


def shim_host() -> str:
    return get("CLOUD_STACK_SHIM_HOST", "127.0.0.1")


def shim_base_url() -> str:
    return f"http://{shim_host()}:{shim_port()}"


def shim_autostart() -> bool:
    return get_bool("CLOUD_STACK_SHIM_AUTOSTART", True)


def image_workers() -> int:
    return max(1, get_int("CLOUD_STACK_IMAGE_WORKERS", 4))


def image_timeout() -> float:
    return get_float("CLOUD_STACK_IMAGE_TIMEOUT", 300.0)


def image_max_reference() -> int:
    """商汤官方调优建议多参考图控制在 2–3 张，默认截断到 3。"""
    return max(1, get_int("CLOUD_STACK_IMAGE_MAX_REFERENCE", 3))


def image_watermark() -> bool:
    """默认关闭：商汤 ``watermark`` 默认为 true，会打上日日新水印。"""
    return get_bool("CLOUD_STACK_IMAGE_WATERMARK", False)


def image_prompt_extend() -> bool:
    """默认关闭：商汤 ``prompt_extend`` 默认为 true，会自动扩写提示词。"""
    return get_bool("CLOUD_STACK_IMAGE_PROMPT_EXTEND", False)


def image_response_format() -> str:
    """``b64_json`` 避免 24 小时失效的临时链接。"""
    return get("CLOUD_STACK_IMAGE_RESPONSE_FORMAT", "b64_json").strip() or "b64_json"


def image_sniff_mime() -> bool:
    """参考图的 data URI 是否按**真实字节**纠正 media type。

    默认开启。这是 2026-09-23 那次「图片功能失效」的修复开关：
    OCV 用扩展名（``mimetypes.guess_type``）声明 data URI 的 media type，
    而商汤按内容校验，遇到「扩展名 .jpg、内容其实是 PNG」的历史参考图
    就确定性 400（``data URL media type does not match the image content``）。

    关掉它只用于把报文还原成上游原样，便于对照排查；
    正式使用不应关闭 —— 关掉后这类参考图的出图会重新失败。
    """
    return get_bool("CLOUD_STACK_IMAGE_SNIFF_MIME", True)


# ---- 云端视频（Agnes AI）----
def video_api_key() -> str:
    """Agnes 视频的 API Key。

    独立字段，不默认复用语言模型的 Key —— 两家是不同平台。
    """
    return get("AGNES_API_KEY")


def video_base_url() -> str:
    return get("AGNES_API_BASE", "https://apihub.agnes-ai.com/v1").rstrip("/")


def video_model() -> str:
    """视频模型 ID。默认 2.5 Flash：当前限时免费，且 v2.0 即将下线。"""
    return get("AGNES_VIDEO_MODEL", "agnes-video-2.5-flash").strip() or "agnes-video-2.5-flash"


def video_enabled() -> bool:
    """视频接管总开关。默认**关闭**。

    与图片 / TTS 不同，视频是**按秒计费**的付费能力，且本适配尚未经过
    真实 Key 的端到端验证（见 ``docs/agnes_video_protocol_gap.py`` 的
    OPEN_QUESTIONS）。默认关闭意味着：不开这个开关，OCV 的视频链路完全
    走它自己的原生配置，插件一行都不插手。
    """
    return get_bool("CLOUD_STACK_VIDEO_ENABLED", False)


def video_inline_media() -> bool:
    """参考媒体是否内联成 data URI 提交（默认开）。

    官方视频文档只承诺「公网可访问的 URL」并要求避免「带本地网络地址」，
    而 OCV 只能给本地文件。Agnes 的**图像**接口明确接受 data URI，视频接口
    未明说 ⇒ 先按内联实现，置 0 可切到严格模式用于排查。
    """
    return get_bool("CLOUD_STACK_VIDEO_INLINE_MEDIA", True)


def video_mode() -> str:
    """模式推导策略：``auto`` / ``text`` / ``keyframe`` / ``reference``。"""
    raw = get("CLOUD_STACK_VIDEO_MODE", "auto").strip().lower()
    return raw if raw in {"auto", "text", "keyframe", "reference"} else "auto"


def video_submit_retries() -> int:
    """上游暂时性拒绝（503 队列满 / 429）时的**最多重试次数**（不含首次提交），默认 30。

    ## 为什么默认带重试（F-013）

    实测（2026-09-24 日志）：95 次受理 / 3 次被拒，**3 次全是同一个**
    ``503 video queue is full, please retry later``。上游自己写着
    "please retry later"，而 OCV 把提交失败当**终态**、一次都不重试
    ⇒ 用户连续几个镜头失败。

    ## 为什么不会重复扣费

    只有 ``AgnesVideoError.retryable``（上游**确定未受理**）才延后重试，
    那时任务没被创建。"状态不明"（网络中断 / 200 却没给视频身份）
    一律当场报错、交给 OCV 冻结镜头，绝不自动重发。

    ## 为什么放在后台而不是同步重试

    OCV 的提交 POST 带 ``timeout=120``，装不下十几分钟的重试窗口。
    所以 shim **立刻**返回一个任务身份，由后台轮询线程按
    :func:`video_submit_retry_delay` 的**阶梯退避**节奏重试；
    OCV 侧会持续查询 30 分钟，足够重试做完。
    """
    return max(0, min(60, get_int("CLOUD_STACK_VIDEO_SUBMIT_RETRIES", 30)))


# --------------------------------------------------------------------------
# 阶梯退避档位表（**单一真相源**，视频与语言模型共用）
# --------------------------------------------------------------------------
# ``(该档覆盖的重试序号上界, 间隔秒数)``。
#
# 用户指定（2026-09-24 视频、2026-09-25 语言模型）：重试 1-3 次每 1 秒、
# 4-8 次每 5 秒、9-12 次每 10 秒、13-15 次每 15 秒、16-20 次每 30 秒、
# 21-25 次每 45 秒、26-30 次每 1 分钟。合计 **788 秒（约 13.1 分钟）**。
#
# ## 为什么提到这里做共用
#
# 视频与语言模型**必须完全一致**（用户明确要求"和视频机制类似"）。
# 各写一份必然漂移 —— 这个仓库已经为"两份清单漂移"付过代价（见 F-006：
# 面板前端硬编码了第二份分页清单）。所以只此一份，两边都从这里取。
#
# ## 边界重叠按「先出现的档位优先」处理
#
# 用户原文里 ``8`` 同时出现在「4-8」与「8-12」、``12`` 同时出现在
# 「8-12」与「12-15」中。若按字面两边都算，第 8、12 次会被赋两个不同的
# 间隔值。这里取**先出现的档位**（第 8 次用 5 秒、第 12 次用 10 秒），
# 得到一个连续无空洞的 30 次序列 —— 自检对这个序列有逐项断言。
LADDER_RETRY_TIERS: tuple[tuple[int, float], ...] = (
    (3, 1.0),
    (8, 5.0),
    (12, 10.0),
    (15, 15.0),
    (20, 30.0),
    (25, 45.0),
    (30, 60.0),
)

# 向后兼容别名（视频侧历史命名）。
SUBMIT_RETRY_TIERS = LADDER_RETRY_TIERS


def ladder_retry_delay(retry_ordinal: int) -> float:
    """第 ``retry_ordinal`` 次重试**之前**应等待的秒数（序号从 1 开始）。

    超出最后一档时沿用最后一档的间隔（用户设了更大的上限时不会变成 0 等待，
    否则会变成密集轰炸上游）。
    """
    ordinal = max(1, int(retry_ordinal))
    for upper, delay in LADDER_RETRY_TIERS:
        if ordinal <= upper:
            return delay
    return LADDER_RETRY_TIERS[-1][1]


def ladder_retry_window(retries: int) -> float:
    """跑完 ``retries`` 次重试所需的**总等待秒数**。"""
    count = max(0, int(retries))
    return sum(ladder_retry_delay(index) for index in range(1, count + 1))


def ladder_retry_schedule() -> str:
    """给人看的档位说明（面板帮助文本、日志、README 共用，避免多处各写一份）。"""
    parts: list[str] = []
    lower = 1
    for upper, delay in LADDER_RETRY_TIERS:
        label = f"{delay:.0f}秒" if delay < 60 else "1分钟"
        parts.append(f"{lower}-{upper} 次每 {label}")
        lower = upper + 1
    return "；".join(parts)


def video_submit_retry_delay(retry_ordinal: int) -> float:
    """（视频）第 N 次重试前的等待秒数。委托给共用的 :func:`ladder_retry_delay`。"""
    return ladder_retry_delay(retry_ordinal)


def video_submit_retry_window(retries: int | None = None) -> float:
    """（视频）跑完 ``retries`` 次重试的总等待秒数。

    这个值必须能装进 OCV 的轮询预算，见 ``video_shim._retry_deadline``。
    """
    count = video_submit_retries() if retries is None else max(0, int(retries))
    return ladder_retry_window(count)


def video_submit_retry_schedule() -> str:
    """（视频）给人看的档位说明。"""
    return ladder_retry_schedule()


def video_size() -> str:
    """输出分辨率档位：720P / 1080P / 1K / 2K。

    Agnes **没有 480p**，而 OCV 的分辨率枚举只有 480p/720p，
    所以以面板值为准，不照抄 OCV 传来的值。
    """
    return get("CLOUD_STACK_VIDEO_SIZE", "720P").strip().upper() or "720P"


def video_poll_seconds() -> float:
    """shim 轮询上游的间隔。官方建议 1–2 秒。"""
    return max(1.0, get_float("CLOUD_STACK_VIDEO_POLL_SECONDS", 2.0))


def video_timeout() -> float:
    """单个视频任务的最长等待（秒）。默认 30 分钟。"""
    return max(60.0, get_float("CLOUD_STACK_VIDEO_TIMEOUT", 1800.0))


# --------------------------------------------------------------------------
# 派生注入状态的自愈
# --------------------------------------------------------------------------

def auto_reinject() -> bool:
    """OCV 后端启动时，是否自动补回被软件更新抹掉的注入物。

    默认开启。OCV 每次更新的 ``update.json`` 里 ``protected_data`` 保护
    ``.env`` / ``runtime`` / ``third-party plugins``，但**不保护** ``frontend/``
    —— 那是会被整体替换的源码目录，于是注入在 ``frontend/index.html``
    里的面板按钮每次更新都会消失（2026-09-15、2026-09-16 两次更新都发生了，
    用户的水印插件也是同一受害者）。

    关闭方式：``.env`` 或面板里设 ``CLOUD_STACK_AUTO_REINJECT=0``。
    自愈是幂等的、只写插件自己的标记块，且失败只记日志，不影响 OCV 启动。
    """
    return get_bool("CLOUD_STACK_AUTO_REINJECT", True)


# --------------------------------------------------------------------------
# Jet Hub（把 dsh-codearts-auth 的免费 LLM provider 接进 OCV）
#
# 全部落在插件自己的 `state/` 目录里，**不桥接 DSH、不读 `~/.dsh`**。
# --------------------------------------------------------------------------


def jethub_enabled() -> bool:
    """Jet Hub 总开关。关掉后桥不会被拉起，OCV 完全看不到这个 provider。"""
    return get_bool("JETHUB_ENABLED", True)


def jethub_provider() -> str:
    """当前启用的 provider id。

    默认 `buddy`（腾讯 CodeBuddy）—— 它是 vendor 里适配器最成熟的同源实现，
    且 CodeBuddy / WorkBuddy 共用同一后端与协议，将来扩展只需在
    `jethub/runtime.mjs` 的 `PROVIDER_SPECS` 里加一行。
    """
    return get("JETHUB_PROVIDER", "buddy").strip() or "buddy"


def jethub_bridge_port() -> int:
    """本机 OpenAI 兼容桥的端口。默认 8801（避开 OCV 8010 / Vite 5173 / shim 8799）。"""
    return get_int("JETHUB_BRIDGE_PORT", 8801)


def jethub_autostart() -> bool:
    """是否由 OCV 后端进程自动拉起桥。"""
    return get_bool("JETHUB_AUTOSTART", True)


def jethub_bridge_timeout_ms() -> int:
    """桥处理单次补全的软超时（毫秒）。

    默认 110000 = 110 秒，**刻意略低于 OCV 的 120 秒读超时**
    （``GEMINI_READ_TIMEOUT_SECONDS`` 默认 120）：让桥有机会返回一个
    结构化的 503（OCV 会按可重试状态码处理），而不是让 OCV 拿到断连。
    """
    return max(5000, get_int("JETHUB_BRIDGE_TIMEOUT_MS", 110_000))


def jethub_model() -> str:
    """语言模型分页选中的云端 OAuth 模型。

    **形如 `<provider>::<model>`**（例如 `buddy::deepseek-v4-flash`）——
    因为不同提供商的模型 id 会重名，必须带提供商前缀桥才能正确路由。
    详见 `models_catalog._fetch_jethub` 与 `jethub/bridge.mjs` 的 `parseModelRef`。
    """
    return get("JETHUB_MODEL", "").strip()


def jethub_bridge_base_url() -> str:
    """桥的 OpenAI 兼容基址（供 OCV 当服务商用）。

    刻意从 `jethub_bridge_port()` 拼而不是硬编码 —— 面板可以改端口，
    这里必须跟着变，否则 OCV 会指着一个不存在的端口。
    """
    return f"http://127.0.0.1:{jethub_bridge_port()}/v1"


def llm_source() -> str:
    """语言模型来源：`custom`（自行填 API）或 `jethub`（云端 OAuth）。

    **两路互斥** —— 用户明确要求：「语言模型和云端 OAuth 两项都是 LLM，
    不能同时使用，在语言模型中给用户下拉选择」。

    这个值是唯一的真相源，最终决定 `LANGUAGE_PROVIDER` 取哪个 provider
    （见 `patches._sync_language_provider`）。
    """
    raw = get("LLM_SOURCE", "custom").strip().lower()
    return "jethub" if raw == "jethub" else "custom"


def resolved_language_provider() -> str:
    """把 `LLM_SOURCE` 翻译成 OCV 侧的 provider id。

    面板、配置视图、`.env` 三方都读它，避免「界面显示用云端 OAuth、
    实际请求打到商汤」这种错配。
    """
    return "jethub" if llm_source() == "jethub" else "sensenova"


def jethub_llm_fallback_models() -> list[dict[str, str]]:
    """离线首屏的静态回退模型表。

    CodeBuddy 实测可用的免费档模型。和 `models_catalog` 的既有约定一致：
    **每一项都必须是实测能用的**，宁可短也不许编。
    在线拉取成功后会以真实清单为准。
    """
    raw = get("JETHUB_LLM_FALLBACK", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                items = []
                for entry in parsed:
                    if isinstance(entry, dict) and str(entry.get("value") or "").strip():
                        items.append(
                            {
                                "value": str(entry["value"]).strip(),
                                "label": str(entry.get("label") or entry["value"]).strip(),
                            }
                        )
                if items:
                    return items
        except (ValueError, TypeError):
            pass
    return [
        {"value": "deepseek-v4-flash", "label": "DeepSeek V4 Flash（CodeBuddy·快）"},
        {"value": "glm-5.2", "label": "GLM-5.2（CodeBuddy）"},
        {"value": "kimi-k2.7", "label": "Kimi K2.7（CodeBuddy）"},
    ]


def jethub_backup_dir() -> Path:
    """备份文件目录（`<插件>/state/backups/`）。"""
    return plugin_var_dir().parent / "state" / "backups"


def jethub_state_dir() -> Path:
    """桥的全部运行时状态目录（凭据 + 账号池 + 备份）。

    这是「所有文件都在插件目录内」这条硬约束的**唯一落点**：
    凭据 `credentials.json`、账号池 `jet-hub/state.json`、备份 `backups/`。
    """
    return plugin_var_dir().parent / "state"
