"""OCV 全云端免费栈插件 —— 安装 / 卸载 / 自检。

用法（在项目根目录下，用 OCV 自带的解释器执行）::

    runtime\\python\\python.exe plugins\\cloud_free_stack\\cloud_stack_ctl.py install
    runtime\\python\\python.exe plugins\\cloud_free_stack\\cloud_stack_ctl.py check
    runtime\\python\\python.exe plugins\\cloud_free_stack\\cloud_stack_ctl.py status
    runtime\\python\\python.exe plugins\\cloud_free_stack\\cloud_stack_ctl.py uninstall

``install`` 只改 ``.env``（配置文件），不碰任何源码；``.env`` 会在改动前
备份成 ``.env.cloud_stack_backup``，卸载时原样还原。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ocv_cloud_stack import asr_prewarm, config, console, shim_launcher  # noqa: E402

BEGIN_MARK = "# ===== OCV 全云端免费栈插件 cloud_free_stack BEGIN（由 cloud_stack_ctl.py 维护）====="
END_MARK = "# ===== OCV 全云端免费栈插件 cloud_free_stack END ====="

BACKUP_SUFFIX = ".cloud_stack_backup"

# 注入锚点：往 OCV 自带解释器的 site-packages 里放一个 .pth。
# CPython 处理 .pth 的时机（addsitepackages）早于 execsitecustomize()，
# 所以这里只要把插件目录塞进 sys.path，紧接着 site.py 就会自动导入我们的
# sitecustomize.py —— 无论用户双击哪个启动入口、甚至直接跑 OCV_Launcher.exe
# 都同样生效，不需要改 OCV 任何源码或启动脚本。
#
# ⚠⚠ .pth 必须是**纯 ASCII** —— 这是踩过的坑，别再加回中文：
#   CPython 的 site.addpackage() 用 locale 编码读 .pth，不是 UTF-8。
#   Lib/site.py 源码注释原话："locale encoding is not ideal especially on Windows.
#   But we have used it for a long time."
#   中文 Windows 的 locale 是 GBK，只要文件里出现一个 GBK 编不出的字节，
#   **整行都读不到**并抛 UnicodeDecodeError。表现是启动器的环境体检直接失败：
#     [失败] 便携路径检查：UnicodeDecodeError: 'gbk' codec can't decode
#            byte 0x89 in position 143: illegal multibyte sequence
#   那个 0x89 就是旧版注释里全角括号「）」的第三个字节。
#   更麻烦的是它**不会**在 OCV 日志里报错 —— 注入静默失效，插件根本没加载。
SITECUSTOMIZE_PTH_NAME = "ocv_cloud_free_stack.pth"

# .pth 要能被这些编码安全解码。全是 ASCII 时任何编码都等价，
# 所以实际校验的就是「是不是纯 ASCII」。
_PTH_SAFE_ENCODINGS = ("ascii", "utf-8", "gbk", "cp1252", "latin-1")

# 前端注入：往 OCV 的 SPA 入口页塞一行 <script>，右下角就会出现「云端免费栈」按钮。
# 这是插件唯一会碰到的 OCV 前端文件，改动是**带标记的一行**，卸载时按标记整段移除。
# 前端由 vite dev server 直接从 frontend/ 提供，所以改 index.html 会即时生效（HMR）。
FRONTEND_MARK_BEGIN = "<!-- cloud_free_stack BEGIN（由 cloud_stack_ctl.py 维护，卸载时自动移除） -->"
FRONTEND_MARK_END = "<!-- cloud_free_stack END -->"
FRONTEND_TARGETS = ("frontend/index.html",)

# 插件自己管理的键（写在带标记的块里）
_BLOCK_KEYS: list[tuple[str, str]] = [
    ("CLOUD_STACK_ENABLED", "1"),
    ("CLOUD_STACK_DEBUG", "0"),
    # OCV 每次软件更新都会整体替换 frontend/，把注入在 index.html 里的面板
    # 按钮抹掉（.env / runtime / 第三方插件都在启动器的 protected_data 里，
    # frontend 不在）。开着这个开关，后端启动时会自动把按钮补回来。
    ("CLOUD_STACK_AUTO_REINJECT", "1"),
    ("CLOUD_STACK_SHIM_PORT", "8799"),
    ("CLOUD_STACK_SHIM_AUTOSTART", "1"),
    ("CLOUD_STACK_IMAGE_WORKERS", "4"),
    ("CLOUD_STACK_IMAGE_MAX_REFERENCE", "3"),
    ("CLOUD_STACK_IMAGE_WATERMARK", "0"),
    ("CLOUD_STACK_IMAGE_PROMPT_EXTEND", "0"),
    ("CLOUD_STACK_IMAGE_RESPONSE_FORMAT", "b64_json"),
    ("MIMO_API_KEY", ""),
    ("MIMO_TTS_BASE_URL", "https://api.xiaomimimo.com/v1"),
    ("MIMO_TTS_MODEL", "mimo-v2.5-tts"),
    ("MIMO_TTS_VOICE", "冰糖"),
    ("MIMO_TTS_RETRIES", "3"),
    # 留空 = 使用插件内置的默认映射表（32 条，已按男女声分好）。
    # 用户一旦在配置面板里改过，这里会被写成完整的映射 JSON。
    ("CLOUD_STACK_VOICE_MAP", ""),
    ("SENSENOVA_API_KEY", ""),
    ("SENSENOVA_API_BASE", "https://token.sensenova.cn/v1"),
    ("SENSENOVA_MODEL", "deepseek-v4-pro"),
    # 输出上限：**留空/0 = 不限制，由模型自身默认值决定**（F-017）。
    #
    # 原先写死 16384，结果长文案在 `director_prompt_editor.finalize_prompts`
    # （一次性输出全部海报提示词，数量随文案线性增长）**必然**撞满上限被截断：
    # `GeminiOutputTruncated: completion_tokens=16384`。
    # 而服务商 `/models` 的 `max_output_length` 不可信（deepseek-flash 声明
    # 65536、真实 393216，少报 6 倍；各模型还不同），端点没有真值可读
    # ⇒ 唯一可靠的"按模型自动"就是**不指定**，让模型用自己的默认
    # （实测省略时上游给 65536，是原值的 4 倍）。
    # 需要精确控制成本/时长时，再填一个正整数（语义为"抬下限"）。
    ("SENSENOVA_MAX_TOKENS", ""),
    ("SENSENOVA_IMAGE_API_BASE", ""),
    # 留空 = 与语言模型共用同一个 Key；面板里可给图片单独配（独立额度/账号）。
    ("SENSENOVA_IMAGE_API_KEY", ""),
    ("SENSENOVA_IMAGE_MODEL", "sensenova-u1.5-lite"),
    ("SENSENOVA_IMAGE_EDIT_MODEL", ""),
    # ---- 本地 ASR（模块 2）的启动健壮性 ----
    # 这两个键不属于"云端"链路，但和它同源：模块 2 是替换完 TTS / 规划 / 出图
    # 之后唯一留在本地的重活，而它的依赖自检只有 90 秒超时。
    # ctranslate2 会连带拉起 6.9 GB 的 torch CUDA DLL，冷启动经常超过 90 秒
    # （详见 ocv_cloud_stack/asr_prewarm.py）。所以：
    #   * 预热把冷启动成本挪到后端启动时；
    #   * 超时抬高只是兜底 —— 它只在"导入很慢"时才会触发，"没装依赖"是
    #     立刻返回非零退出码的，不会白等。
    ("CLOUD_STACK_ASR_PREWARM", "1"),
    ("CLOUD_STACK_ASR_PREWARM_TIMEOUT", "900"),
    ("ASR_RUNTIME_CHECK_TIMEOUT_SECONDS", "600"),
]

# 需要被"顶掉"的 OCV 原生键：安装时改，卸载时还原
_OVERRIDDEN_KEYS = ("LANGUAGE_PROVIDER", "IMAGE_API_BASE_URL", "IMAGE_MODEL_ID", "DASHSCOPE_API_KEY")

# 视频接管要顶掉的 OCV 原生键。
#
# **刻意不放进 `_OVERRIDDEN_KEYS`**，而是由 `video-install` 单独写入 ——
# 视频是按秒计费的付费能力（见 config.video_enabled 的说明），必须显式 opt-in；
# 而且用户很可能已经配了真实的视频服务商，`install` 不该悄悄改掉那套配置。
_VIDEO_OVERRIDDEN = (
    "VIDEO_API_BASE_URL", "VIDEO_SUBMIT_PATH", "VIDEO_QUERY_PATH",
    "VIDEO_UPLOAD_PATH", "VIDEO_API_KEY", "VIDEO_RESOLUTION",
)

# 交给 OCV 的"已配置"占位 Key。
#
# 关键：OCV 会拿这个值做 `Authorization: Bearer <KEY>` 打到 **shim**，
# 而 shim 用面板里的真 Key 去调 Agnes。所以这里只需非空即可 ——
# 与 DASHSCOPE_API_KEY 顶 qwen 槽位是同一套做法。
# 把一个显眼的占位串写进去，是为了让用户在 OCV 的视频设置页一眼看出
# "这条链路被插件接管了"，而不是看见一个来路不明的假 Key。
_VIDEO_PLACEHOLDER_KEY = "managed-by-cloud-free-stack"

# 视频提交路径。模型段用 `[^/]+` 在 shim 侧匹配，所以这里写死一个稳定的
# 段名即可 —— 用户改 AGNES_VIDEO_MODEL 时不需要同步改这个路径。
VIDEO_SUBMIT_PATH_VALUE = "/openapi/v2/video/agnes/multimodal-video"
VIDEO_QUERY_PATH_VALUE = "/api/video/query"
# 上传端点故意指向一个 shim **不实现**的地址：OCV 的视频 provider 有内置回退，
# 上传失败会把参考图转成 data URI 直传（见 module6_dynamic_video.upload_image）。
VIDEO_UPLOAD_PATH_VALUE = "/openapi/v2/media/upload/binary"

# 插件块里记录「这个原生键被改写前的原值」的前缀，卸载时据此精确还原。
PREV_PREFIX = "CLOUD_STACK_PREV_"


# --------------------------------------------------------------------------
# .env 读写
# --------------------------------------------------------------------------

def _env_path() -> Path:
    return config.project_root() / ".env"


def _read_lines() -> list[str]:
    path = _env_path()
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _write_lines(lines: list[str]) -> None:
    path = _env_path()
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def _has_key(lines: list[str], key: str) -> bool:
    """`.env` 里**存在**这个键吗（不论值是否为空）。

    必须与 ``_parse_value`` 区分开：后者对「键不存在」和「键存在但值为空」
    都返回空串，而这在还原语义上是**两件完全不同的事**：

    * 键不存在 ⇒ 我们不知道原值 ⇒ 安全阀：保留现值，绝不静默清空；
    * 键存在但为空 ⇒ 原值本来就是空 ⇒ 必须清空（否则会把插件自己注入的
      值，例如指向死 shim 的 ``IMAGE_API_BASE_URL=http://127.0.0.1:8799``，
      当成"用户设置"永久留下）。
    """
    prefix = f"{key}="
    for line in lines:
        stripped = line.strip().lstrip("\ufeff")
        if stripped.startswith("#"):
            continue
        if stripped.startswith(prefix):
            return True
    return False


def _parse_value(lines: list[str], key: str) -> str:
    prefix = f"{key}="
    for line in lines:
        # ⚠ 先剥掉行首的 BOM（U+FEFF）。本仓的 .env 是 **UTF-8 with BOM**，
        #   而 `decode("utf-8")` 会把 BOM 保留成第一行的一个普通字符，
        #   于是 `stripped.startswith("LANGUAGE_PROVIDER=")` 对**第一行**恒为假
        #   —— 那个键会被当成"不存在"。
        #   实测踩过（2026-09-23，见 DEVLOG F-004）：一份首行即键的 .env 在
        #   两次 install 后，PREV_LANGUAGE_PROVIDER 记录成了空串，卸载时无法
        #   还原用户原值。真实 .env 首行恰好是注释所以侥幸没出事，但那是运气。
        stripped = line.strip().lstrip("\ufeff")
        if stripped.startswith("#") or not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return ""


def _set_key(lines: list[str], key: str, value: str) -> list[str]:
    """把 ``key`` 设为 ``value``，并**清除该键的所有重复行**。

    只替换第一次出现是不够的：install 会把被顶掉的键改写**在插件块之外**
    （``LANGUAGE_PROVIDER`` 等原本就在文件里的位置），而卸载还原时又可能
    在别处再写一行 —— 于是同一个键在文件里出现两次，``_parse_value`` 取
    第一个、第二个成为**陈旧残留**。实测踩过（2026-09-23，见 DEVLOG F-004）：
    卸载后 ``LANGUAGE_PROVIDER=custom``（正确）与 ``LANGUAGE_PROVIDER=sensenova``
    （旧的插件注入值）并存，读的值取决于用哪套解析，极易再次踩坑。
    因此这里做**去重**：保留一处，其余删除。
    """
    prefix = f"{key}="
    kept: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().lstrip("\ufeff").startswith(prefix):
            if not replaced:
                kept.append(f"{key}={value}")
                replaced = True
            # 重复行直接丢弃
            continue
        kept.append(line)
    if not replaced:
        kept.append(f"{key}={value}")
    return kept


def _strip_block(lines: list[str]) -> list[str]:
    kept: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == BEGIN_MARK:
            inside = True
            continue
        if line.strip() == END_MARK:
            inside = False
            continue
        if not inside:
            kept.append(line)
    return kept


def _render_block(existing: dict[str, str]) -> list[str]:
    block = [BEGIN_MARK]
    for key, default in _BLOCK_KEYS:
        # .env 里已有非空值就沿用，避免覆盖用户填好的 Key
        value = existing.get(key) or ""
        current = _parse_value(_read_lines(), key)
        if current:
            value = current
        elif not value:
            value = default
        block.append(f"{key}={value}")

    # 记录被顶掉的原生键的**原始值**。
    #
    # ⚠ 这里必须优先沿用**已有的** PREV 记录，不能无条件读磁盘当前值。
    #   重复 install 时磁盘上的值已经是**插件上一次注入的值**（例如
    #   IMAGE_API_BASE_URL=http://127.0.0.1:8799），再拿它当"原值"记下来，
    #   卸载时就会"还原"成插件自己的注入值 —— 用户真实的
    #   IMAGE_API_BASE_URL 被永久丢失，且还原结果指向一个已经不存在的 shim。
    #   实测复现（2026-09-23）：两次 install 后
    #       PREV_IMAGE_API_BASE_URL  custom 值 -> http://127.0.0.1:8799
    #   只有 PREV 记录**不存在**时，磁盘当前值才等于用户原值。
    disk = _read_lines()
    for key in _OVERRIDDEN_KEYS:
        recorded = _parse_value(disk, _prev_key(key))
        if recorded:
            value = recorded
        else:
            value = _parse_value(disk, key)
        block.append(f"{_prev_key(key)}={value}")
    block.append(END_MARK)
    return block


def _backup_env() -> Path:
    source = _env_path()
    target = source.with_name(source.name + BACKUP_SUFFIX)
    if source.is_file() and not target.is_file():
        shutil.copy2(source, target)
    return target


def _prev_key(key: str) -> str:
    """被顶掉的 OCV 原生键在插件块里留下的原始值记录键名。"""
    return f"{PREV_PREFIX}{key}"


def snapshot_keys(source: Path, keys: Iterable[str]) -> dict[str, str]:
    """从某个 .env 快照里取出指定键的值（不存在则记为缺失）。"""
    if not source.is_file():
        return {}
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    return {key: _parse_value(lines, key) for key in keys}


def restore_keys_from_snapshot(source: Path, keys: Iterable[str]) -> list[tuple[str, str, str]]:
    """把 ``keys`` 的值从快照 ``source`` 恢复到当前 ``.env``。

    **只动这几个键，其余内容（包括插件块）原样保留。**
    这是 uninstall 与手工修复共用的原语：早期实现是 ``shutil.copy2(快照, .env)``
    整文件覆盖，而那份快照是**首次安装时的一次性副本**；用户后来对 .env 做的
    任何改动（换模型、改 ASR_DEVICE、填 Key）都会在卸载时被静默冲掉。
    实测踩过一次（2026-09-23）：`ASR_DEVICE=cpu` 被打回 `auto`、
    `DASHSCOPE_API_KEY` 被换成占位值。

    返回 ``[(key, 旧值, 新值), ...]`` 便于打印与断言。
    """
    target = _env_path()
    values = snapshot_keys(source, keys)
    lines = _read_lines()
    changes: list[tuple[str, str, str]] = []
    for key, value in values.items():
        old = _parse_value(lines, key)
        if old == value:
            continue
        lines = _set_key(lines, key, value)
        changes.append((key, old, value))
    if changes:
        _write_lines(lines)
    return changes


def _restore_preserving_block() -> list[str]:
    """卸载时的定向还原：撤掉插件块 + 恢复被顶掉的键，其它内容一律不动。

    每个被顶掉的键，按下面的优先级决定卸载后该是什么值：

    | 情况 | 处理 | 理由 |
    |---|---|---|
    | ① ``CLOUD_STACK_PREV_*`` 记录**非空** | 还原成记录值 | 那是安装当时读到的用户原值，最权威 |
    | ② 记录为空 **且键存在** | 还原成**空** | 原值本来就是空 ⇒ 必须清掉插件注入的值 |
    | ③ 记录为空 **且键不存在** | 保留磁盘当前值 | 不知道原值 ⇒ 安全阀，宁留勿删 |
    | ④ 无记录，但备份快照里有非空值 | 还原成快照值 | 回落来源 |

    ⚠ ②③ 的区分是关键，曾经写错过：把"记录为空"一律当成"要保留现值"，
    结果卸载后 ``IMAGE_API_BASE_URL`` 仍指向 ``http://127.0.0.1:8799``
    （一个卸载后就没人监听的地址），用户以为卸干净了、实际 OCV 的出图全废。
    判断"键是否存在"必须用 :func:`_has_key`，不能用 ``_parse_value``。

    返回被"因缺原值而保留"的键名列表，供调用方提示。
    """
    original = _read_lines()
    lines = _strip_block(original)
    backup_path = _env_path().with_name(_env_path().name + BACKUP_SUFFIX)
    backup_lines = backup_path.read_text(encoding="utf-8", errors="replace").splitlines() \
        if backup_path.is_file() else []

    kept_nonempty: list[str] = []
    for key in (*_OVERRIDDEN_KEYS, *_VIDEO_OVERRIDDEN):
        recorded = _parse_value(original, _prev_key(key))
        if recorded:
            lines = _set_key(lines, key, recorded)          # ①
            continue
        snapshot = _parse_value(backup_lines, key)
        if snapshot:
            lines = _set_key(lines, key, snapshot)          # ④
            continue
        if _has_key(original, key):
            lines = _set_key(lines, key, "")                # ② 原值本就是空
            continue
        # ③ 键不存在且无记录：保留现值（若现值非空，提示用户）
        if _parse_value(original, key):
            kept_nonempty.append(key)
    _write_lines(lines)
    return kept_nonempty



# --------------------------------------------------------------------------
# 注入锚点（.pth）
# --------------------------------------------------------------------------

def _site_packages_dir() -> Path | None:
    candidate = config.project_root() / "runtime" / "python" / "Lib" / "site-packages"
    return candidate if candidate.is_dir() else None


def _pth_path() -> Path | None:
    target = _site_packages_dir()
    return (target / SITECUSTOMIZE_PTH_NAME) if target else None


def _pth_line(plugin_dir: str) -> str:
    """构造一行**纯 ASCII** 的 .pth 注入语句。

    路径里若含非 ASCII 字符（用户把整合包解到「E:\\一键成片」这种目录），
    用 ``\\uXXXX`` 转义 —— 于是路径带中文时 .pth 本体依然是 ASCII，
    一样不受 locale 编码影响。纯 ASCII 路径则保留可读的 raw 字符串写法。

    注意转义顺序：先把反斜杠翻倍（否则非 raw 字符串里 ``\\1`` 会被当成八进制
    转义 ``\\x01``），再转义非 ASCII —— 两者都只作用于各自那一类字符，不会互相干扰。
    """
    comment = "  # OCV cloud_free_stack injection anchor - removed on uninstall"
    if plugin_dir.isascii():
        return f"import sys; sys.path.insert(0, r'{plugin_dir}'){comment}"
    escaped = plugin_dir.replace("\\", "\\\\")
    escaped = "".join(
        char if char.isascii() else "\\u%04x" % ord(char) for char in escaped
    )
    return f"import sys; sys.path.insert(0, '{escaped}'){comment}"


def _pth_text_is_safe(text: str) -> bool:
    """这行文本能不能被所有候选编码解出来？（等于问：是不是纯 ASCII）"""
    for encoding in _PTH_SAFE_ENCODINGS:
        try:
            text.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            return False
    return True


def anchor_encoding_ok() -> bool | None:
    """当前锚点是否编码安全？``None`` = 没安装。"""
    pth = _pth_path()
    if pth is None or not pth.is_file():
        return None
    try:
        pth.read_bytes().decode("ascii")
        return True
    except (OSError, UnicodeDecodeError):
        return False


def install_injection() -> Path | None:
    """写入 .pth，让 OCV 自带的解释器自动加载本插件。

    写之前先自检编码安全（原因见 ``SITECUSTOMIZE_PTH_NAME`` 上面的长注释）：
    一个非 ASCII 的锚点会让注入**静默失效**，而报错信息是启动器的
    「便携路径检查失败」，很难联想到插件头上 —— 所以宁可在这里拦住。
    """
    pth = _pth_path()
    if pth is None:
        print("  ⚠ 找不到 runtime/python/Lib/site-packages，跳过 .pth 注入")
        return None
    line = _pth_line(str(config.plugin_root()))
    if not _pth_text_is_safe(line):
        print("  ✗ 注入锚点无法做成纯 ASCII，已放弃写入（否则中文 Windows 下注入会失效）")
        return None
    # encoding="ascii" 是第二道保险：万一 _pth_line 回归成非 ASCII，
    # 这里会直接抛 UnicodeEncodeError，而不是默默写下一个坏锚点。
    pth.write_text(line + "\n", encoding="ascii")
    return pth


def remove_injection() -> bool:
    pth = _pth_path()
    if pth is None or not pth.is_file():
        return False
    pth.unlink()
    return True


# --------------------------------------------------------------------------
# 前端注入（配置面板入口按钮）
# --------------------------------------------------------------------------

def _frontend_targets() -> list[Path]:
    targets: list[Path] = []
    for relative in FRONTEND_TARGETS:
        path = config.project_root() / relative
        if path.is_file():
            targets.append(path)
    return targets


def _panel_script_block() -> str:
    return (
        f"{FRONTEND_MARK_BEGIN}\n"
        f'<script src="{config.shim_base_url()}/panel.js" defer></script>\n'
        f"{FRONTEND_MARK_END}"
    )


def _strip_frontend_block(html: str) -> str:
    """按标记整段移除。用正则而不是字符串查找，避免标签间空白差异导致漏删。"""
    import re

    pattern = re.compile(
        re.escape(FRONTEND_MARK_BEGIN) + r".*?" + re.escape(FRONTEND_MARK_END) + r"\s*",
        re.DOTALL,
    )
    return pattern.sub("", html)


def _write_frontend_atomic(path: Path, text: str) -> None:
    """原子写回 frontend 入口。

    以前直接 ``path.write_text(...)``，它是「先截断、再写」。同一个后端的
    另一个插件的自愈线程（ocv_watermark 也往 index.html 注入自己的一行）
    正好在这两个动作之间读到文件，就会拿到 0 字节 —— 然后它按「空文件」
    去插入，把整份 Vite 模板覆盖成几十个字节。实测过一次，模板就是这么没的。

    字节语义与 ``Path.write_text(text, encoding="utf-8")`` 保持一致：
    文本里的 ``\\n`` 归一成平台换行。
    """
    data = text.encode("utf-8")
    if os.linesep != "\n":
        data = data.replace(b"\n", os.linesep.encode("ascii"))
    pending = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:10]}.pending")
    try:
        pending.write_bytes(data)
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def _frontend_lock_path() -> Path:
    return config.project_root() / "plugins" / ".ocv_frontend_inject.lock"


@contextlib.contextmanager
def _frontend_lock(timeout: float = 15.0):
    """抢前端注入锁，再动 ``frontend/index.html``。

    原子写只保证「读不到半截文件」，挡不住「两个读-改-写后写的覆盖先写的」。
    ``ocv_watermark`` 也往同一个文件注入自己的一行，两边都在后端启动时自愈，
    撞上就会丢一方的注入。约定是：动这个文件前先抢
    ``plugins/.ocv_frontend_inject.lock``（``O_CREAT|O_EXCL`` 抢占 + 僵尸锁超时）。

    实现与 ``plugins/ocv_watermark/host/reapply.py`` 的 ``frontend_lock``
    **是镜像，路径与协议必须同步改**。
    """
    lock_path = _frontend_lock_path()
    deadline = time.time() + timeout
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 20.0:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                break
            time.sleep(0.05)
        except OSError:
            break  # 建不了锁就别卡住：宁可偶发丢一次，也不能让 install 挂住
    try:
        if fd is not None:
            try:
                os.write(fd, f"{os.getpid()} {time.time():.3f}\n".encode("ascii"))
            except OSError:
                pass
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            lock_path.unlink(missing_ok=True)


def inject_frontend() -> list[Path]:
    changed: list[Path] = []
    block = _panel_script_block()
    with _frontend_lock():
        for path in _frontend_targets():
            html = path.read_text(encoding="utf-8", errors="replace")
            if not html.strip():
                # 读到别人写入的中间态（空/半截）。绝不在残缺内容上做读-改-写。
                continue
            cleaned = _strip_frontend_block(html)
            if "</body>" in cleaned:
                updated = cleaned.replace("</body>", f"{block}\n</body>", 1)
            else:
                updated = cleaned.rstrip() + "\n" + block + "\n"
            if updated != html:
                _write_frontend_atomic(path, updated)
                changed.append(path)
    return changed


def remove_frontend() -> list[Path]:
    changed: list[Path] = []
    with _frontend_lock():
        for path in _frontend_targets():
            html = path.read_text(encoding="utf-8", errors="replace")
            if not html.strip():
                continue
            updated = _strip_frontend_block(html)
            if updated != html:
                _write_frontend_atomic(path, updated)
                changed.append(path)
    return changed


def frontend_injected() -> bool:
    for path in _frontend_targets():
        try:
            if FRONTEND_MARK_BEGIN in path.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def open_panel(browser: bool = True) -> bool:
    """打开配置面板（前提是 shim 已被某个长驻进程托管）。"""
    url = f"{config.shim_base_url()}/panel"
    if not shim_launcher.is_listening():
        print(f"✗ shim 不可用，无法打开面板。可手工访问：{url}")
        return False
    print(f"✓ 配置面板：{url}")
    if browser:
        try:
            if hasattr(os, "startfile"):
                os.startfile(url)  # type: ignore[attr-defined]
            else:
                import webbrowser

                webbrowser.open(url)
        except OSError:
            print("  （自动打开浏览器失败，请手工复制上面的地址）")
    return True


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------

def cmd_install(args: argparse.Namespace) -> int:
    env_path = _env_path()
    if not env_path.is_file():
        print(f"✗ 找不到 .env：{env_path}")
        return 1

    backup = _backup_env()
    print(f"· 已备份 .env -> {backup.name}")

    lines = _read_lines()
    previous = {key: _parse_value(lines, key) for key in _OVERRIDDEN_KEYS}

    lines = _strip_block(lines)
    lines = _set_key(lines, "LANGUAGE_PROVIDER", "sensenova")
    lines = _set_key(lines, "IMAGE_API_BASE_URL", config.shim_base_url())
    lines = _set_key(lines, "IMAGE_MODEL_ID", config.sensenova_image_model())
    if not previous.get("DASHSCOPE_API_KEY"):
        # MiMo 接管了 qwen 槽位，这里只是让 OCV 的存在性校验通过
        lines = _set_key(lines, "DASHSCOPE_API_KEY", "mimo-managed-by-cloud-free-stack")

    print()
    for key in _OVERRIDDEN_KEYS:
        old = previous.get(key) or "（空）"
        print(f"· {key}: {old}  ->  {_parse_value(lines, key) or '（空）'}")

    # 先把 _strip_block 留下的尾部空行清干净，再固定补一个分隔空行。
    # 不这么做的话，块前空行会每跑一次 install 多攒一行 —— 反复 install
    # （排查时很常见）会看到 .env 每次都被改动，看起来像「配置没生效」。
    while lines and not lines[-1].strip():
        lines.pop()
    block = _render_block({})
    lines.extend(["", *block])
    _write_lines(lines)

    pth = install_injection()
    if pth is not None:
        print(f"· 已写入注入锚点 {pth.relative_to(config.project_root())}")

    touched = inject_frontend()
    for path in touched:
        print(f"· 已在前端入口注入面板按钮 {path.relative_to(config.project_root())}")

    print(f"\n✓ 配置已写入 {env_path}")
    print("\n  下一步 —— 填 MiMo 与商汤的 API Key：")
    print("    A) 启动 OCV（双击启动.bat），点界面右下角「云端免费栈」按钮，或")
    print(f"    B) 直接运行：runtime\\python\\python.exe plugins\\cloud_free_stack\\cloud_stack_ctl.py panel")
    print(f"       面板地址：{config.shim_base_url()}/panel")
    print("\n  装好后用 OCV 平时的入口启动即可（双击启动.bat / OCV_Launcher.exe），")
    print("  插件会在每个子进程启动时自动挂载，不需要改任何启动方式。")
    return 0


def cmd_video_install(args: argparse.Namespace) -> int:
    """把 OCV 的视频链路指向本地 shim（**必须显式执行，install 不含此步**）。

    ## 为什么不做进 `install`

    视频是**按秒计费**的付费能力（Agnes 720P 约 $0.025/秒），而且用户很可能
    已经配了真实的视频服务商（RunningHub、自建 ComfyUI…）。`install` 顺手改掉
    这套配置属于"未经同意就换了用户的付费通道"，不可接受。所以视频接管是
    **显式 opt-in**：跑这个命令才会改。

    ## 改了什么

    把 OCV 视频 provider 读的那 6 个键指向本地 shim，并在插件块里记录原值
    （`CLOUD_STACK_PREV_*`），使 `video-uninstall` 能精确还原。

    ## 为什么不写真实 Agnes Key 进 .env

    OCV 会拿 `VIDEO_API_KEY` 直接发 `Authorization: Bearer <KEY>` 给
    **shim**，shim 再用面板里的真 Key 调 Agnes。所以这里只放一个占位串，
    真 Key 留在面板层（`var/config.json`）—— 与图片层把 Key 放面板是同一套。
    """
    env_path = _env_path()
    if not env_path.is_file():
        print(f"✗ 找不到 .env：{env_path}")
        return 1

    lines = _read_lines()
    block_present = any(line.strip() == BEGIN_MARK for line in lines)
    if not block_present:
        print("✗ 插件尚未安装（.env 里没有插件块）。请先运行 install。")
        return 1

    # 记录原值。**只为"原本就存在"的键写 PREV 记录** —— 这一点很关键：
    # 卸载时用「PREV 记录是否存在」判断"接管前有没有这个键"，
    # 若给不存在的键也写一条空的 PREV，卸载就只能还原成 `KEY=` 空行、
    # 而无法知道应该整行删掉（实测踩过：卸载后 .env 多出 6 个空键，
    # 与接管前不再逐字节相同）。
    additions: dict[str, str] = {}
    print("== 接管 OCV 视频链路 ==")
    for key in _VIDEO_OVERRIDDEN:
        if _parse_value(lines, _prev_key(key)):
            continue  # 已有原值记录，保持不动（见 F-004①）
        if not _has_key(lines, key):
            continue  # 原本没这个键 ⇒ 不写记录 ⇒ 卸载时整行删掉
        additions[_prev_key(key)] = _parse_value(lines, key)

    _backup_env()
    lines = _set_key(lines, "VIDEO_API_BASE_URL", config.shim_base_url())
    lines = _set_key(lines, "VIDEO_SUBMIT_PATH", VIDEO_SUBMIT_PATH_VALUE)
    lines = _set_key(lines, "VIDEO_QUERY_PATH", VIDEO_QUERY_PATH_VALUE)
    lines = _set_key(lines, "VIDEO_UPLOAD_PATH", VIDEO_UPLOAD_PATH_VALUE)
    lines = _set_key(lines, "VIDEO_RESOLUTION", "720p")
    # 占位 Key：只要非空即可。真 Key 在面板层。
    _set_if_blank = _parse_value(lines, "VIDEO_API_KEY")
    if not _set_if_blank or _set_if_blank == _VIDEO_PLACEHOLDER_KEY:
        lines = _set_key(lines, "VIDEO_API_KEY", _VIDEO_PLACEHOLDER_KEY)

    # 把 PREV 记录补进插件块（块内、END 之前）
    if additions:
        index = next((i for i, line in enumerate(lines) if line.strip() == END_MARK), None)
        if index is None:
            print("✗ 插件块结构异常（找不到 END 标记），未做任何修改")
            return 1
        for offset, (key, value) in enumerate(additions.items(), start=1):
            lines.insert(index + offset, f"{key}={value}")

    _write_lines(lines)

    for key in _VIDEO_OVERRIDDEN:
        shown = _parse_value(lines, key)
        if key == "VIDEO_API_KEY" and shown:
            shown = shown[:12] + "…" if len(shown) > 12 else shown
        print(f"  {key:22s} = {shown or '（空）'}")
    print("\n✓ OCV 视频链路已指向本地 shim")
    print(f"  上游：{config.video_base_url()}  模型：{config.video_model()}")
    print(f"  开关：CLOUD_STACK_VIDEO_ENABLED = {config.video_enabled()}（1 才真正接管）")
    print("\n  还需：")
    print("    1. 在面板「云端视频」分页填 Agnes API Key（AGNES_API_KEY）")
    print("    2. 把 CLOUD_STACK_VIDEO_ENABLED 设为 1")
    print("    3. 重启 OCV 让配置生效")
    print("\n  还原：cloud_stack_ctl.py video-uninstall")
    return 0


def _remove_key(lines: list[str], key: str) -> list[str]:
    """整行删除某个键（含重复行）。"""
    prefix = f"{key}="
    return [line for line in lines
            if not line.strip().lstrip("\ufeff").startswith(prefix)]


def cmd_video_uninstall(args: argparse.Namespace) -> int:
    """还原 OCV 视频链路的 6 个键，其它内容不动。

    还原语义与 `_restore_preserving_block` 一致（三态）：

    * PREV 记录非空 → 还原成记录值
    * 记录空 + 备份快照有值 → 用快照值
    * 记录空 + 快照空 + **原文件里本来没有这个键** → **整行删除**
      （而不是留一个 `KEY=` 空行 —— 那会让 `.env` 与接管前不再逐字节相同，
      实测踩过：卸载后多出 6 个空键）
    """
    env_path = _env_path()
    if not env_path.is_file():
        print(f"✗ 找不到 .env：{env_path}")
        return 1

    original = _read_lines()
    backup = env_path.with_name(env_path.name + BACKUP_SUFFIX)
    backup_lines = backup.read_text(encoding="utf-8", errors="replace").splitlines() \
        if backup.is_file() else []

    # 判断"这个键在接管之前是否存在"：PREV 记录存在即说明接管时它就在。
    had_before = {
        key: _has_key(original, _prev_key(key)) for key in _VIDEO_OVERRIDDEN
    }

    lines = original
    restored: list[tuple[str, str]] = []
    for key in _VIDEO_OVERRIDDEN:
        recorded = _parse_value(original, _prev_key(key))
        snapshot = _parse_value(backup_lines, key)
        if recorded:
            lines = _set_key(lines, key, recorded)
            restored.append((key, recorded))
        elif snapshot:
            lines = _set_key(lines, key, snapshot)
            restored.append((key, snapshot))
        elif had_before[key]:
            # 接管前就存在该键、但原值为空 ⇒ 还原成空行
            lines = _set_key(lines, key, "")
            restored.append((key, ""))
        else:
            # 接管前根本没有这个键 ⇒ 整行删掉，让 .env 回到原状
            lines = _remove_key(lines, key)
            restored.append((key, "<已移除>"))

    # 清掉 PREV 记录
    lines = [line for line in lines
             if not any(line.strip().lstrip("\ufeff").startswith(f"{_prev_key(k)}=")
                        for k in _VIDEO_OVERRIDDEN)]

    if lines != original:
        _write_lines(lines)
        print("== 还原 OCV 视频链路 ==")
        for key, value in restored:
            shown = value if len(value) <= 24 else f"{value[:10]}…{value[-4:]}"
            print(f"  √ {key:22s} = {shown or '（空）'}")
    else:
        print("· 视频链路无需还原（未接管或已还原）")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    """卸载：撤掉插件的一切痕迹，并还原被顶掉的 OCV 原生键。

    **不再整文件覆盖 ``.env``。** 早期实现是
    ``shutil.copy2(.env.cloud_stack_backup, .env)``，而那份备份是
    **首次安装时的一次性快照**：用户此后对 ``.env`` 的任何改动
    （换模型、调 ``ASR_DEVICE``、填 Key）都会在卸载时被静默冲掉。
    实测踩过一次（2026-09-23，见 DEVLOG F-003）。

    现在的做法是**定向还原**：只处理插件自己管的两类东西 ——
    ① 插件块整段移除；② 被顶掉的 4 个原生键按 ``CLOUD_STACK_PREV_*``
    记录（缺失时回落备份快照）还原。其余内容一个字节都不碰。
    """
    env_path = _env_path()
    before = env_path.read_bytes() if env_path.is_file() else b""
    kept = _restore_preserving_block()
    after = env_path.read_bytes() if env_path.is_file() else b""
    if before != after:
        print("✓ 已移除 .env 中的插件块，并还原被顶掉的 OCV 原生键")
        # 保留一份卸载前副本，便于回溯（不覆盖首次安装快照）
        try:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            keep = env_path.with_name(f"{env_path.name}.uninstalled_{stamp}")
            keep.write_bytes(before)
            print(f"· 卸载前副本已留存：{keep.name}")
        except OSError:
            pass
    else:
        print("· .env 无插件痕迹，未改动")
    for key in kept:
        print(f"! {key} 的原值记录为空，已保留你当前设置的值（未清空）")

    print("✓ 已删除注入锚点" if remove_injection() else "· 注入锚点不存在")
    removed = remove_frontend()
    if removed:
        for path in removed:
            print(f"✓ 已从 {path.relative_to(config.project_root())} 移除面板按钮")
    else:
        print("· 前端未注入过面板按钮")
    if shim_launcher.stop():
        print("✓ 已停止本地图片 shim")
    print("· 面板覆盖层保留在 plugins/cloud_free_stack/var/config.json（如需清空请手工删除该文件）")
    return 0


def cmd_restore_keys(args: argparse.Namespace) -> int:
    """从指定 .env 快照定向恢复被插件顶掉的 OCV 原生键。

    用途：早期版本的 ``uninstall`` 会整文件覆盖 ``.env``（用首次安装时的
    一次性快照），把用户后来改的键冲掉。这个命令按**键**恢复，不碰其它内容。

        cloud_stack_ctl.py restore-keys --from .env.endpoint_backup
        cloud_stack_ctl.py restore-keys --from .env.endpoint_backup --keys ASR_DEVICE
    """
    source_name = str(getattr(args, "from_file", "") or "").strip()
    if not source_name:
        print("✗ 需要 --from <快照文件名>（例如 .env.endpoint_backup）")
        return 1
    source = config.project_root() / source_name
    if not source.is_file():
        print(f"✗ 快照不存在：{source}")
        return 1

    raw_keys = str(getattr(args, "keys", "") or "").strip()
    if raw_keys:
        keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
    else:
        # 默认恢复"插件会顶掉/插件会管、但装到 .env 块外"的键
        keys = list(_OVERRIDDEN_KEYS) + ["ASR_DEVICE", "ASR_MODEL", "ASR_LANGUAGE",
                                         "ASR_RUNTIME_CHECK_TIMEOUT_SECONDS"]

    print(f"== 从 {source.name} 定向恢复 ==")
    changes = restore_keys_from_snapshot(source, keys)
    if not changes:
        print("· 无需恢复（当前值与快照一致）")
        return 0
    for key, old, new in changes:
        shown_old = old if len(old) <= 20 else f"{old[:6]}…{old[-4:]}"
        shown_new = new if len(new) <= 20 else f"{new[:6]}…{new[-4:]}"
        print(f"✓ {key}: {shown_old or '（空）'}  ->  {shown_new or '（空）'}")
    print(f"\n共恢复 {len(changes)} 个键（其余内容未改动）")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    lines = _read_lines()
    print("== 插件配置（.env 基线）==")
    for key, _ in _BLOCK_KEYS:
        value = _parse_value(lines, key)
        shown = value
        if key.endswith("_KEY") and value:
            shown = f"{value[:6]}…{value[-4:]}" if len(value) > 12 else "（已设置）"
        print(f"  {key:32s} = {shown or '（空）'}")

    store = config.store_values()
    print(f"\n== 面板覆盖层（{len(store)} 个键）==")
    if not store:
        print("  （空，全部沿用 .env 与默认值）")
    for key in sorted(store):
        value = store[key]
        if "API_KEY" in key and value:
            value = f"{value[:6]}…{value[-4:]}" if len(value) > 12 else "（已设置）"
        print(f"  {key:32s} = {value[:60]}")

    print("\n== OCV 原生键（被插件改写）==")
    for key in _OVERRIDDEN_KEYS:
        print(f"  {key:32s} = {_parse_value(lines, key) or '（空）'}")

    # ASR 是 OCV 原生能力，插件不改写它，但它决定「要不要白试一次 CUDA」。
    # 注意 .env 里「键不存在」与「键存在但值为空」对 module2 是两种结果：
    # 前者回落默认值，后者会因为空串不在 {auto,cuda,cpu} 里直接抛 ValueError。
    print("\n== ASR 识别（OCV 原生键，插件不改写）==")

    def _asr_value(key: str, default: str) -> tuple[str, str]:
        """返回值与口径备注，区分 未设置 / 空值 / 有值 三种情形。"""
        present = any(line.strip().startswith(f"{key}=") for line in lines)
        value = _parse_value(lines, key).strip()
        if present and not value:
            return "（空值）", "  ⚠ module2 会按非法配置报错，请删掉该行或填合法值"
        if not present:
            return default, "  （.env 未设置，用默认）"
        return value, ""

    device, note = _asr_value("ASR_DEVICE", "auto")
    if not note:
        note = {
            "cpu": "  直接走 CPU/int8，不探测 CUDA",
            "auto": "  先试 CUDA/float16，失败再回退 CPU（无 N 卡会白试一次）",
            "cuda": "  强制 CUDA，无 N 卡会直接失败，不回退",
        }.get(device.lower(), "  ⚠ 非法值，只接受 auto/cuda/cpu")
    print(f"  {'ASR_DEVICE':32s} = {device}{note}")
    for _key, _default in (("ASR_MODEL", "base"), ("ASR_LANGUAGE", "zh")):
        _value, _note = _asr_value(_key, _default)
        print(f"  {_key:32s} = {_value}{_note}")

    print("\n== 运行时 ==")
    pth = _pth_path()
    injected = bool(pth and pth.is_file())
    anchor_note = ""
    if injected:
        # 锚点非纯 ASCII 时，中文 Windows（locale=GBK）下 site.addpackage()
        # 会解不开 .pth，注入静默失效 —— 这里必须显式告警。
        anchor_note = (
            "  纯 ASCII，编码安全"
            if anchor_encoding_ok()
            else "  ⚠ 含非 ASCII：GBK 环境下注入会失效，请重跑 install 修复"
        )
    print(f"  自动注入锚点    : {'已安装' if injected else '未安装'}"
          + (f"  {pth.relative_to(config.project_root())}" if injected and pth else "")
          + anchor_note)
    frontend_ok = frontend_injected()
    print(f"  前端面板按钮    : {'已注入' if frontend_ok else '未注入'}"
          + ("" if frontend_ok else "  ⚠ 软件更新会抹掉它；运行 reinject 可重建（或启动 OCV 自动重建）"))
    print(f"  更新后自愈      : {'开启' if config.auto_reinject() else '已关闭（CLOUD_STACK_AUTO_REINJECT=0）'}")
    listening = shim_launcher.is_listening()
    print(f"  图片 shim       : {'运行中' if listening else '未运行'}  {config.shim_base_url()}")
    if listening:
        info = shim_launcher.health() or {}
        mode = {"inproc": "由 OCV 进程内托管", "detached": "独立进程"}.get(
            str(info.get("mode") or ""), "未知"
        )
        print(f"  托管方式        : {mode}  pid={info.get('pid', '?')}")
        print(f"  配置面板        : {config.shim_base_url()}/panel")
        print(f"  活跃生图任务    : {info.get('tasks', '?')}")
    else:
        print("  提示            : 启动 OCV（后端会托管 shim）或运行 panel / check 命令")
    print(f"  MiMo 模型       : {config.mimo_model()}  音色 {config.mimo_default_voice()}")
    print(f"  商汤 LLM        : {config.sensenova_model()} @ {config.sensenova_base_url()}")
    print(f"  商汤 图片       : {config.sensenova_image_model()} @ {config.sensenova_image_base_url()}")
    print(f"  音色映射条数    : {len(config.voice_map())}")
    print(f"  项目根          : {config.project_root()}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    import base64
    import io

    import requests

    failures = 0

    print("== 1/4 配置完整性 ==")
    store = config.store_values()
    print(f"  面板覆盖层      : {len(store)} 个键  {config.store_path()}")
    if not config.mimo_api_key():
        print("  ✗ 未配置 MiMo API Key —— 在配置面板的「MiMo 配音」页填写")
        failures += 1
    else:
        source = config.layer_of("MIMO_API_KEY")
        where = {"panel": "配置面板", "environ": ".env（进程环境）", "env": ".env 文件"}.get(source, source)
        print(f"  ✓ MiMo API Key 已设置（{len(config.mimo_api_key())} 字符，来自{where}）")
    if not config.sensenova_api_key():
        print("  ✗ 未配置商汤 API Key —— 在配置面板的「商汤语言模型」页填写")
        failures += 1
    else:
        source = config.layer_of("SENSENOVA_API_KEY")
        where = {"panel": "配置面板", "environ": ".env（进程环境）", "env": ".env 文件"}.get(source, source)
        print(f"  ✓ 商汤 API Key 已设置（{len(config.sensenova_api_key())} 字符，来自{where}）")

    env_base = _parse_value(_read_lines(), "IMAGE_API_BASE_URL").rstrip("/")
    if env_base != config.shim_base_url():
        print(f"  ✗ IMAGE_API_BASE_URL={env_base or '（空）'}，应为 {config.shim_base_url()}（先运行 install）")
        failures += 1
    else:
        print(f"  ✓ IMAGE_API_BASE_URL 已指向本机 shim")

    if frontend_injected():
        print("  ✓ 前端面板入口已注入（OCV 界面右下角「云端免费栈」按钮）")
    else:
        print("  ! 前端面板入口未注入 —— 运行 reinject 可重建（不影响 OCV 本身运行）")

    print("\n== 2/4 商汤 LLM 连通性（会消耗极少量额度）==")
    if config.sensenova_api_key():
        try:
            response = requests.post(
                f"{config.sensenova_base_url()}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {config.sensenova_api_key()}",
                },
                json={
                    "model": config.sensenova_model(),
                    "messages": [
                        {"role": "system", "content": "只输出 JSON。"},
                        {"role": "user", "content": '返回 {"ok": true}'},
                    ],
                    "max_tokens": 256,
                    "response_format": {"type": "json_object"},
                },
                timeout=(15, 120),
            )
            if response.ok:
                body = response.json()
                content = str(body["choices"][0]["message"].get("content") or "")[:80]
                print(f"  ✓ HTTP {response.status_code} 返回: {content}")
            else:
                print(f"  ✗ HTTP {response.status_code}: {response.text[:300]}")
                failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {type(exc).__name__}: {exc}")
            failures += 1
    else:
        print("  - 跳过（无 Key）")
        failures += 1

    print("\n== 3/4 MiMo TTS 连通性（会消耗极少量额度）==")
    if config.mimo_api_key():
        try:
            response = requests.post(
                f"{config.mimo_base_url()}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "api-key": config.mimo_api_key(),
                    "Authorization": f"Bearer {config.mimo_api_key()}",
                },
                json={
                    "model": config.mimo_model(),
                    "messages": [{"role": "assistant", "content": "测试"}],
                    "audio": {"format": "wav", "voice": config.mimo_default_voice()},
                },
                timeout=(15, 120),
            )
            if response.ok:
                body = response.json()
                audio = (body.get("choices") or [{}])[0].get("message", {}).get("audio") or {}
                blob = str(audio.get("data") or "")
                size = len(base64.b64decode(blob)) if blob else 0
                print(f"  ✓ HTTP {response.status_code} 音频 {size} 字节")
                if size < 128:
                    print("  ⚠ 音频数据过小，请检查音色名或模型名")
                    failures += 1
            else:
                print(f"  ✗ HTTP {response.status_code}: {response.text[:300]}")
                failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {type(exc).__name__}: {exc}")
            failures += 1
    else:
        print("  - 跳过（无 Key）")
        failures += 1

    print("\n== 4/4 商汤图片链路（经本地 shim，会消耗额度）==")
    if args.skip_image:
        print("  - 已跳过（--skip-image）")
    elif not config.sensenova_api_key():
        print("  - 跳过（无 Key）")
        failures += 1
    else:
        if not shim_launcher.is_listening():
            # 自检自己就要打这个端口，所以在本进程里托管即可 —— 不依赖任何外部进程
            print("  · shim 未运行，在本进程内临时托管…")
            shim_launcher.ensure_in_process()
        if not shim_launcher.is_listening():
            print("  ✗ shim 启动失败")
            failures += 1
        else:
            try:
                submitted = requests.post(
                    f"{config.shim_base_url()}/openapi/v2/{config.sensenova_image_model()}/text-to-image",
                    json={"prompt": "一张纯色测试图", "aspectRatio": "16:9", "resolution": "1k"},
                    timeout=30,
                ).json()
                task_id = (submitted.get("data") or {}).get("taskId")
                print(f"  · 已提交任务 {task_id}")
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    result = requests.post(
                        f"{config.shim_base_url()}/openapi/v2/query",
                        json={"taskId": task_id},
                        timeout=30,
                    ).json()
                    data = result.get("data") or {}
                    status = str(data.get("status") or "")
                    if status == "SUCCESS":
                        print(f"  ✓ 出图成功: {(data.get('results') or [{}])[0].get('url')}")
                        break
                    if status == "FAILED":
                        print(f"  ✗ 出图失败: {data.get('errorMessage')}")
                        failures += 1
                        break
                    time.sleep(3)
                else:
                    print("  ✗ 等待超时")
                    failures += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  ✗ {type(exc).__name__}: {exc}")
                failures += 1

    print()
    if failures:
        print(f"✗ 自检完成，{failures} 项需要处理")
        return 1
    print("✓ 自检全部通过")
    return 0


def cmd_shim(args: argparse.Namespace) -> int:
    from ocv_cloud_stack import image_shim

    return image_shim.serve_forever(port=args.port)


def cmd_stop(args: argparse.Namespace) -> int:
    if shim_launcher.stop():
        print("✓ 已停止独立运行的 shim")
        return 0
    if shim_launcher.is_listening():
        print("· 端口上还有 shim，但它是被某个 OCV 进程托管的（没有独立 pid）。")
        print("  关闭 OCV（停止后端）即可一并停止；这里不会去 kill 后端进程。")
        return 0
    print("· shim 未在运行")
    return 0


def cmd_panel(args: argparse.Namespace) -> int:
    """打开配置面板（MiMo 配音 / 商汤模型）。"""
    if not args.no_browser and not frontend_injected():
        touched = inject_frontend()
        for path in touched:
            print(f"· 前端未注入，已补上 {path.relative_to(config.project_root())}")

    if shim_launcher.is_listening():
        return 0 if open_panel(browser=not args.no_browser) else 1

    # 没有托管者（通常是因为 OCV 后端还没起来）。此时不能"起个进程就退出"，
    # 那样面板一刷新就挂了 —— 只能在本窗口前台托管，用完由用户 Ctrl+C 结束。
    from ocv_cloud_stack import image_shim

    print("· 未检测到 shim 托管进程（OCV 后端没在运行时会这样）。")
    print("  本窗口将临时托管 shim 与配置面板，面板用完后按 Ctrl+C 结束本命令。")
    if not args.no_browser:
        def _open_later() -> None:
            import time as _time

            _time.sleep(1.5)
            open_panel(browser=True)

        threading.Thread(target=_open_later, daemon=True).start()
    return image_shim.serve_forever()


def cmd_inject(args: argparse.Namespace) -> int:
    """只处理前端注入，不动 .env。"""
    if args.remove:
        removed = remove_frontend()
        print(f"✓ 已从 {len(removed)} 个前端入口移除面板按钮" if removed else "· 前端未注入过")
        return 0
    touched = inject_frontend()
    if not touched:
        print("· 前端注入已是目标状态（或找不到 frontend/index.html）")
    for path in touched:
        print(f"✓ 已注入 {path.relative_to(config.project_root())}")
    return 0


def cmd_reinject(args: argparse.Namespace) -> int:
    """重新注入被软件更新抹掉的派生状态（前端按钮 / .pth 锚点）。

    与 ``install`` 的区别：``install`` 还会写 ``.env``（插件配置块 + 顶掉
    原生键），适合首次部署；``reinject`` 只碰**派生层** —— 那两个东西是
    OCV 每次更新都会清掉、又完全可以原地重建的注入物。幂等，可反复运行。
    """
    del args
    from ocv_cloud_stack import reapply

    return reapply.run(verbose=True)


def cmd_prewarm(args: argparse.Namespace) -> int:
    """手工预热 ASR 运行时（模块 2 的依赖自检）。"""
    del args
    print("== 预热 ASR 运行时（模块 2）==")
    print("  目的：模块 2 的依赖自检有 90 秒超时，而 ctranslate2 会连带拉起")
    print("        6.9 GB 的 torch CUDA DLL，冷加载常常超过它。")
    return asr_prewarm.report()


def main(argv: list[str] | None = None) -> int:
    # 控制台编码加固：中文 Windows 的 stdout 是 GBK，打印 ✓ / ✗ / ⚠ 会抛
    # UnicodeEncodeError 并终止进程。插件被禁用时 bootstrap 不会走到，
    # 所以这个控制台命令自己兜一层。
    console.harden()

    parser = argparse.ArgumentParser(
        prog="cloud_stack_ctl.py",
        description="OCV 全云端免费栈插件控制台（MiMo-TTS + 商汤 LLM + 商汤图片）",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("install", help="写入插件配置、注入锚点与前端面板入口")
    sub.add_parser("uninstall", help="还原 .env、移除注入并停止 shim")
    sub.add_parser("status", help="查看配置与运行时状态")
    check = sub.add_parser("check", help="逐项验证三个云端链路")
    check.add_argument("--skip-image", action="store_true", help="跳过高成本的出图测试")
    panel = sub.add_parser("panel", help="打开配置面板（MiMo / 商汤）")
    panel.add_argument("--no-browser", action="store_true", help="只打印地址，不打开浏览器")
    inject = sub.add_parser("inject", help="只处理前端面板入口的注入/移除")
    inject.add_argument("--remove", action="store_true", help="移除注入")
    sub.add_parser(
        "reinject",
        help="软件更新后重建被抹掉的注入（前端按钮 + .pth 锚点），幂等",
    )
    shim = sub.add_parser("shim", help="在前台运行图片 shim（调试用）")
    shim.add_argument("--port", type=int, default=None)
    sub.add_parser("stop", help="停止后台 shim")
    sub.add_parser("prewarm", help="预热 ASR 运行时（消除模块 2 的冷启动超时）")
    restore = sub.add_parser(
        "restore-keys",
        help="从某个 .env 快照定向恢复被插件顶掉的 OCV 原生键（不动其它内容）",
    )
    restore.add_argument("--from", dest="from_file", default="", help="快照文件名，如 .env.endpoint_backup")
    restore.add_argument("--keys", default="", help="逗号分隔的键名；缺省恢复一组常用键")
    sub.add_parser(
        "video-install",
        help="把 OCV 视频链路指向本地 shim（按秒计费，需显式 opt-in）",
    )
    sub.add_parser(
        "video-uninstall",
        help="还原 OCV 视频链路的 6 个原生键",
    )

    args = parser.parse_args(argv)
    handlers = {
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "check": cmd_check,
        "panel": cmd_panel,
        "inject": cmd_inject,
        "reinject": cmd_reinject,
        "shim": cmd_shim,
        "stop": cmd_stop,
        "prewarm": cmd_prewarm,
        "restore-keys": cmd_restore_keys,
        "video-install": cmd_video_install,
        "video-uninstall": cmd_video_uninstall,
    }
    handler = handlers.get(args.command or "")
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
