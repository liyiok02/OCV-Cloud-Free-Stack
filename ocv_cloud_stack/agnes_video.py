"""Agnes AI 视频模型客户端 —— 把 OCV 的请求翻译成 Agnes 的异步视频协议。

## 为什么需要这一层

OCV 的视频层（``module6_dynamic_video.RunningHubVideoProvider``）只讲一种协议：
**RunningHub 风格的异步任务** —— ``POST <submit_path>`` 拿 ``taskId``，再
``POST <query_path>`` 带 ``{"taskId": ...}`` 轮询，从响应里递归找 http(s) URL。

Agnes 的视频协议在**七个环节**上都不一样（逐条实测自官方文档，见
``docs/agnes_video_protocol_gap.py`` 的对照表）：

======================  ==================================  ==========================================
环节                    OCV 期望                            Agnes 实际
======================  ==================================  ==========================================
创建任务                ``POST <submit_path>``              ``POST /v1/videos``
请求体                  ``{prompt, duration, imageUrls…}``   ``{model, prompt, seconds, mode, size…}``
任务标识                响应 ``taskId``                     响应有 ``id``/``task_id``/``video_id``，
                                                           而**查询只认 video_id**
查询方式                ``POST <query_path> {taskId}``       ``GET /agnesapi?video_id=…&model_name=…``
状态取值                大写 ``SUCCESS``/``FAILED``          小写 ``completed``/``failed``
结果地址                ``results[].url``                    顶层 ``url``
时长/分辨率             ``duration`` 整数 4–15；480p/720p     ``seconds`` **字符串** "4"–"12"；
                                                            ``size`` 720P/1080P/1K/2K（**无 480p**）
======================  ==================================  ==========================================

与其去改 OCV（违背零改动原则、且会被软件更新覆盖），这里做**协议翻译**：
对外讲 RunningHub 协议，对内讲 Agnes 协议。这与图片层
（``image_shim`` 把 RunningHub 的异步出图翻译成商汤的同步接口）是同一套思路。

## 三个必须显式处理的语义鸿沟

1. **模式（``mode``）**：Agnes 强制三选一且媒体字段互斥。
   OCV 没有这个概念，它给的是「一句提示词 + 1～9 张参考图，第 1 张是核心分镜」。
   本模块按图片数量推导（见 :func:`plan_mode`）：

   * 0 张 → ``text``
   * 1 张 → ``keyframe`` + ``first_frame``（**核心分镜就是真实首帧**，
     对「让分镜图动起来」这个 OCV 主场景最忠实，也最保角色一致性）
   * 2 张 → ``keyframe`` + ``first_frame`` + ``last_frame``
   * ≥3 张 → ``reference`` + ``images[]``

2. **时长**：OCV 给 4–15 的整数，Agnes 只收 ``"4"``–``"12"`` 的**字符串**。
   夹紧到 12 并转字符串（夹紧是必须的：15 秒会直接被 400 拒掉）。

3. **分辨率**：Agnes 没有 480p。OCV 只可能发 ``480p`` 或 ``720p``，
   两者都映射到面板配置的档位（默认 720P），并在降/升档时记日志，
   不假装 480p 生效了。

## 不确定项（已在文档中标注，不假装已知）

官方视频文档只写「媒体 URL 应当可由 Agnes AI 服务**公开访问**」，
且明确要求「避免…带本地网络地址」；而 OCV 传进来的是本地图或 data URI。
Agnes 的**图像**接口明确接受 data URI（``extra_body.image``），视频接口未明说。
因此本模块**默认按 data URI 提交**（与图像层一致），并留
``CLOUD_STACK_VIDEO_INLINE_MEDIA`` 开关；若上游拒绝，真实错误会原样回传，
不会静默失败。**无 API Key 时无法实测这一点，故据此实现并如实标注。**
"""

from __future__ import annotations

import base64
import json
import mimetypes
import time
from pathlib import Path
from typing import Any

import requests

from . import config


class AgnesVideoError(RuntimeError):
    """调用 Agnes 视频接口失败（网络 / HTTP / 响应结构不可识别）。

    ## 为什么带 ``status_code`` / ``retryable`` / ``definite``（F-013）

    这三个字段让**上层**能正确区分三种失败，而不是一律当成"被拒绝"：

    | 情况 | ``retryable`` | ``definite`` | 含义 |
    |---|---|---|---|
    | HTTP 408/425/429/5xx | 是 | 是 | 上游明确"这次没收下，稍后再来"（如 ``503 video queue is full``） |
    | HTTP 其它 4xx | 否 | 是 | 参数/凭据错误，重试无意义；但**确定没创建任务** |
    | HTTP 200 但没有 ``video_id`` | **否** | **否** | **可能已创建** —— 状态不明 |
    | 网络异常（超时/连接中断） | **否** | **否** | 请求**可能已到达**上游 —— 状态不明 |

    ``definite=True`` 表示"我们**确定**上游没有创建任务"，因此可以安全地
    把这次失败当成**终态**交给用户（他点重试不会重复扣费）。

    ``definite=False``（状态不明）**绝不能**当成终态去自动重试 ——
    那可能就是"已创建但没拿到身份"，重试即重复付费。
    这类必须让上层冻结镜头、由用户去核实。这是本模块最要紧的安全线。
    """

    def __init__(self, message: str, *, status_code: int | None = None,
                 retryable: bool = False, definite: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = bool(retryable)
        self.definite = bool(definite)


# 「上游这次没收下、稍后可以再来」的 HTTP 状态。
#
# 408 请求超时 / 425 Too Early / 429 限流 / 5xx 服务端暂时不可用。
# 注意 500/502/504 也放进来：它们同样意味着**任务未被受理**，
# 而上游对"未受理"的重试是幂等安全的。
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


# 「模式偏好被忽略」只提示一次（见 _log_preference_ignored）。
import threading as _threading

_WARN_LOCK = _threading.Lock()
_TEXT_PREFERENCE_WARNED = False


# Agnes 的状态 -> OCV 认识的大写状态。
# OCV 侧：RUNNING = {RUNNING, QUEUED, PENDING, PROCESSING, SUBMITTED, ""}
#         SUCCESS = {SUCCESS, SUCCEEDED, COMPLETED, COMPLETE, FINISHED}
#         FAILURE = {FAILED, FAILURE, ERROR, ...}
_STATUS_MAP = {
    "queued": "QUEUED",
    "pending": "PENDING",
    "in_progress": "RUNNING",
    "processing": "RUNNING",
    "running": "RUNNING",
    "completed": "SUCCESS",
    "succeeded": "SUCCESS",
    "success": "SUCCESS",
    "failed": "FAILED",
    "failure": "FAILED",
    "error": "FAILED",
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
}

# Agnes 的时长边界（字符串 "4"–"12"）。OCV 允许 4–15，超出的必须夹紧。
MIN_SECONDS = 4
MAX_SECONDS = 12

# 支持的画幅（两边完全一致，只是字段名从 ratio 改成 aspect_ratio）。
SUPPORTED_RATIOS = ("16:9", "9:16", "4:3", "3:4", "1:1", "21:9")

# 各档位允许的输出分辨率。Flash 只支持 720P，且不支持参考视频。
_ALL_SIZES = ("720P", "1080P", "1K", "2K")
_FLASH_SIZES = ("720P",)
_FLASH_MAX_IMAGES = 5


def upstream_base() -> str:
    """Agnes 真实上游地址（注意：不是 OCV 看到的那个，那个指向本地 shim）。"""
    return config.get("AGNES_API_BASE", "https://apihub.agnes-ai.com/v1").rstrip("/")


def video_model() -> str:
    """默认视频模型。

    刻意默认 ``agnes-video-2.5-flash``（当前限时免费）而不是 ``agnes-video-v2.0``
    —— 后者官方公告将于 **2026-09-25 23:59:59 (UTC+8)** 下线。
    """
    return config.get("AGNES_VIDEO_MODEL", "agnes-video-2.5-flash").strip() or "agnes-video-2.5-flash"


def _is_flash(model: str) -> bool:
    return "flash" in str(model or "").lower()


def allowed_sizes(model: str) -> tuple[str, ...]:
    return _FLASH_SIZES if _is_flash(model) else _ALL_SIZES


def output_size(model: str | None = None) -> str:
    """输出分辨率档位。

    Agnes **没有 480p**，而 OCV 的分辨率枚举只有 ``480p``/``720p``，
    所以这里以面板配置为准（默认 720P），而不是照抄 OCV 的值 ——
    照抄会得到非法的 "480p" 并被 400 拒绝。
    """
    model = model or video_model()
    raw = config.get("CLOUD_STACK_VIDEO_SIZE", "720P").strip().upper()
    if raw not in allowed_sizes(model):
        # 模型不支持该档位（例如 Flash 只吃 720P）时回落到 720P，并让调用方记日志。
        return allowed_sizes(model)[0]
    return raw


def inline_media() -> bool:
    """参考媒体是否内联成 data URI 提交（默认是）。

    理由见模块文档的「不确定项」：OCV 只能给本地文件，而 Agnes 要求公网可达。
    置 0 时不做 data URI（那时只有 http(s) 输入能过），用于排查上游是否拒绝内联。
    """
    return config.get_bool("CLOUD_STACK_VIDEO_INLINE_MEDIA", True)


def mode_preference() -> str:
    """模式偏好：``auto`` / ``text`` / ``keyframe`` / ``reference``。"""
    raw = config.get("CLOUD_STACK_VIDEO_MODE", "auto").strip().lower()
    return raw if raw in {"auto", "text", "keyframe", "reference"} else "auto"


# --------------------------------------------------------------------------
# 媒体准备
# --------------------------------------------------------------------------

def media_to_url(value: str) -> str:
    """把一个参考媒体转换 Agnes 能收的形态。

    * 已是 ``http(s)`` → 原样（公网可达，最理想）
    * 已是 ``data:``   → 按需纠正 media type（复用图片层的字节嗅探，见 F-001：
      历史参考图存在「扩展名 .jpg、内容其实是 PNG」，声明不符会被上游 400）
    * 本地路径         → 读字节、嗅探真实类型、编成 data URI
    """
    from . import sense_image  # 延迟导入：复用 F-001 的字节嗅探，不重复实现

    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    if text.startswith("data:"):
        return sense_image._correct_data_uri(text) if config.image_sniff_mime() else text

    path = Path(text)
    if not path.is_file():
        raise AgnesVideoError(f"参考媒体不存在：{path.name}")
    blob = path.read_bytes()
    mime = sense_image.sniff_image_mime(blob) if config.image_sniff_mime() else ""
    if not mime:
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


def prepare_media(references: list[str], model: str) -> tuple[list[str], list[dict[str, str]]]:
    """把参考图列表转成 Agnes 形态，返回 ``(mode, payload 片段)``。

    截断规则按模型区分（Flash 最多 5 张、不支持参考视频），
    截断时**不静默**：调用方会把这个信息写进日志。
    """
    usable = [item for item in (str(x).strip() for x in references) if item]
    limit = _FLASH_MAX_IMAGES if _is_flash(model) else 8
    usable = usable[:limit]
    urls = [] if not inline_media() else [media_to_url(item) for item in usable]

    preference = mode_preference()

    # ⚠ ``text`` 偏好与"本镜有参考图"是**矛盾**的，绝不能静默丢掉参考图。
    #
    # OCV 的动态镜头**永远至少有一张图**（``validate_request`` 强制 1–9 张，
    # 且图 1 就是即将定稿的核心分镜图，整个动态视频的目的就是"让它动起来"）。
    # 而 Agnes 的 ``mode=text`` 契约明写**不允许**任何媒体字段
    # （``text`` 模式下带 ``images``/``first_frame`` 会被 400 拒绝）。
    # 所以一旦在面板把模式钉死成 ``text``，带图镜头就会走进这两个坏结局之一：
    #
    #   * 照发 mode=text + 丢掉图 → 视频与分镜完全不符（**静默产出错数据**）；
    #   * 照发 mode=text + 带上图 → 上游 400，任务失败。
    #
    # 两者都不能接受。正确处理：**有图时不采纳 text 偏好**，退回按图数推导
    # （keyframe/reference），并**明确记日志**说明偏好被忽略 —— 用户设了
    # ``text`` 却拿到 keyframe，必须能从日志看出原因，而不是以为设置生效了。
    if preference == "text" and urls:
        _log_preference_ignored(len(urls))

    if preference != "auto" and not (preference == "text" and urls):
        if preference == "text":
            return "text", {}
        if preference == "keyframe":
            frames: dict[str, Any] = {}
            if urls:
                frames["first_frame"] = urls[0]
            if len(urls) > 1:
                frames["last_frame"] = urls[1]
            if not frames:
                raise AgnesVideoError("keyframe 模式至少需要 1 张参考图")
            return "keyframe", frames
        if not urls:
            raise AgnesVideoError("reference 模式至少需要 1 张参考图")
        return "reference", {"images": urls}

    # auto（或 text 偏好被忽略）：按图片数量推导（见模块文档的三个语义鸿沟）
    if not urls:
        return "text", {}
    if len(urls) == 1:
        return "keyframe", {"first_frame": urls[0]}
    if len(urls) == 2:
        return "keyframe", {"first_frame": urls[0], "last_frame": urls[1]}
    return "reference", {"images": urls}


def _log_preference_ignored(image_count: int) -> None:
    """模式偏好 ``text`` 被有图镜头忽略时，打一条**只能看到一次**的醒目日志。

    用独立函数是为了让它可被自检直接断言（不必去抓 stdout）。
    """
    global _TEXT_PREFERENCE_WARNED
    with _WARN_LOCK:
        if _TEXT_PREFERENCE_WARNED:
            return
        _TEXT_PREFERENCE_WARNED = True
    try:
        from . import image_shim

        image_shim._log(  # noqa: SLF001 - 与其它 shim 共用同一份日志
            f"视频模式偏好被忽略：面板设了「生成模式=text」，但本镜带 {image_count} 张参考图"
            f"（OCV 动态镜头必有核心分镜图）。已按图数自动改用 keyframe/reference，"
            f"否则参考图会被丢弃或遭上游 400 拒绝。如需纯文生视频请改用无参考图的镜头。"
        )
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 请求构造
# --------------------------------------------------------------------------

def build_payload(
    *,
    prompt: str,
    ratio: str = "16:9",
    resolution: str = "720p",
    duration: int = 5,
    seed: int = -1,
    reference_images: list[str] | None = None,
    model: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """构造 Agnes 的创建任务请求体，返回 ``(payload, 提示信息)``。"""
    model = (model or video_model()).strip()
    notes: list[str] = []

    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise AgnesVideoError("Agnes 视频接口收到空提示词")

    # 时长：夹紧到 Agnes 的 "4".."12"，并转字符串。
    try:
        seconds = int(duration)
    except (TypeError, ValueError):
        seconds = 5
    clamped = max(MIN_SECONDS, min(MAX_SECONDS, seconds))
    if clamped != seconds:
        notes.append(f"时长 {seconds}s 超出 Agnes 支持范围 {MIN_SECONDS}–{MAX_SECONDS}s，已夹紧为 {clamped}s")

    # 画幅：两边取值一致，仅字段名不同。
    aspect = str(ratio or "16:9").strip() or "16:9"
    if aspect not in SUPPORTED_RATIOS:
        notes.append(f"画幅 {aspect} 不受 Agnes 支持，已回落 16:9")
        aspect = "16:9"

    size = output_size(model)
    if str(resolution or "").strip().lower() == "480p" and size.upper() != "480P":
        notes.append(f"OCV 请求 480p，但 Agnes 无该档位，已按面板配置输出 {size}")

    mode, media = prepare_media(list(reference_images or []), model)
    payload: dict[str, Any] = {
        "model": model,
        "prompt": clean_prompt,
        # 必须是字符串："4"–"12"。传整数会被 400 拒绝。
        "seconds": str(clamped),
        "mode": mode,
        "size": size,
        "n": 1,
    }
    if mode != "text":
        payload["aspect_ratio"] = aspect
    payload.update(media)

    # seed：OCV 用 -1 表示"随机"。Agnes 的 seed 是可选的整数，
    # 传负数没有意义，故 -1 时不带这个字段。
    try:
        seed_value = int(seed)
    except (TypeError, ValueError):
        seed_value = -1
    if seed_value >= 0:
        payload["seed"] = seed_value

    if _is_flash(model) and len(payload.get("images") or []) > _FLASH_MAX_IMAGES:
        notes.append(f"Flash 模型最多 {_FLASH_MAX_IMAGES} 张参考图，多余已丢弃")

    return payload, notes


def query_url(video_id: str, model: str | None = None) -> str:
    """Agnes 的查询地址。

    **必须带 model_name**：不带 ``model_name`` 的纯 ``video_id`` 查询
    只对 ``mode: "text"`` 有效，``keyframe`` / ``reference`` 会查不到。
    OCV 的提示词通常带参考图（走 keyframe/reference），所以一律带上。
    """
    model = model or video_model()
    from urllib.parse import quote

    return (
        f"{upstream_base()}/agnesapi"
        f"?video_id={quote(str(video_id), safe='')}&model_name={quote(model, safe='')}"
    )


def _http_error(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:300].strip()}"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or ""
            return f"HTTP {response.status_code} {message}".strip()
        if body.get("message"):
            return f"HTTP {response.status_code} {body['message']}"
    return f"HTTP {response.status_code}"


# --------------------------------------------------------------------------
# 对外动作
# --------------------------------------------------------------------------

def create_task(
    payload: dict[str, Any],
    *,
    api_key: str,
    session: requests.Session | None = None,
    timeout: tuple[float, float] = (20.0, 180.0),
) -> dict[str, Any]:
    """提交创建任务，返回 ``{"video_id", "task_id", "status", "raw"}``。

    ## 失败分类（F-013，决定上层能不能重试）

    | 情况 | 抛出的异常 | 重试 | 确定未创建 |
    |---|---|---|---|
    | HTTP 408/425/429/5xx | ``retryable=True`` | 可以 | **是** |
    | HTTP 其它 4xx | ``retryable=False`` | 不可以 | **是**（参数/凭据错） |
    | 网络异常（超时/连接中断） | 两者皆 False | 不可以 | **否**（请求可能已到达） |
    | 200 但响应结构不明 / 无 video_id | 两者皆 False | **绝不可以** | **否**（可能已创建） |

    ⚠ 后两行是安全线：**"状态不明"必须当成既不可重试、也不确定**，
    让用户去核实，而不是赌"大概没创建成功"。见异常类的文档。
    """
    if not str(api_key or "").strip():
        raise AgnesVideoError("未配置 Agnes API Key，无法提交视频任务", definite=True)
    session = session or requests.Session()
    url = f"{upstream_base()}/videos"
    try:
        response = session.post(
            url,
            headers={"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        # 网络层异常：请求**可能**已到达上游 ⇒ **不是**确定未创建。
        # 注意这里**不**标 retryable：自动重发有重复扣费风险，必须让
        # OCV 冻结该镜头、由用户核实（OCV 自己也是这么设计的）。
        raise AgnesVideoError(
            f"Agnes 视频提交网络异常：{type(exc).__name__}: {exc}",
            retryable=False,
            definite=False,
        ) from exc

    if response.status_code >= 400:
        status = int(response.status_code)
        # 4xx/5xx 都意味着上游**确实没收下**这次请求 ⇒ definite=True。
        # 其中 408/425/429/5xx 是"暂时性"，可以安全重试。
        raise AgnesVideoError(
            f"Agnes 视频提交失败：{_http_error(response)}",
            status_code=status,
            retryable=status in _RETRYABLE_STATUS,
            definite=True,
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise AgnesVideoError("Agnes 视频提交返回了非 JSON 内容") from exc
    if not isinstance(body, dict):
        raise AgnesVideoError("Agnes 视频提交返回结构不可识别") from None

    # 查询只认 video_id；id / task_id 只用于识别任务。
    video_id = str(body.get("video_id") or "").strip()
    task_id = str(body.get("id") or body.get("task_id") or "").strip()
    if not video_id and not task_id:
        # ⚠ HTTP 200 但没有身份：**可能已创建**。既不可重试、也不确定。
        raise AgnesVideoError(
            "Agnes 视频提交未返回 video_id/task_id（可能已创建，禁止自动重试）："
            + json.dumps(body, ensure_ascii=False)[:300],
            status_code=200,
            retryable=False,
            definite=False,
        )
    return {
        "video_id": video_id or task_id,
        "task_id": task_id or video_id,
        "status": str(body.get("status") or "queued"),
        "raw": body,
    }


def fetch_task(
    video_id: str,
    *,
    api_key: str,
    model: str | None = None,
    session: requests.Session | None = None,
    timeout: tuple[float, float] = (15.0, 60.0),
) -> dict[str, Any]:
    """查询任务，返回 ``{"status", "url", "error", "raw"}``。

    ``status`` 已映射成 OCV 认识的大写集合。
    """
    if not str(api_key or "").strip():
        raise AgnesVideoError("未配置 Agnes API Key，无法查询视频任务")
    session = session or requests.Session()
    try:
        response = session.get(
            query_url(video_id, model),
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise AgnesVideoError(f"Agnes 视频查询网络异常：{type(exc).__name__}: {exc}") from exc

    if response.status_code >= 400:
        raise AgnesVideoError(f"Agnes 视频查询失败：{_http_error(response)}")

    try:
        body = response.json()
    except ValueError as exc:
        raise AgnesVideoError("Agnes 视频查询返回了非 JSON 内容") from exc
    if not isinstance(body, dict):
        raise AgnesVideoError("Agnes 视频查询返回结构不可识别")

    raw_status = str(body.get("status") or "").strip().lower()
    mapped = _STATUS_MAP.get(raw_status, "UNKNOWN")
    url = str(body.get("url") or "").strip()
    error = body.get("error")
    if isinstance(error, dict):
        error_text = str(error.get("message") or error.get("code") or "")
    else:
        error_text = str(error or "")
    return {"status": mapped, "raw_status": raw_status, "url": url,
            "error": error_text, "raw": body}


def run_until_done(
    *,
    prompt: str,
    api_key: str,
    ratio: str = "16:9",
    resolution: str = "720p",
    duration: int = 5,
    seed: int = -1,
    reference_images: list[str] | None = None,
    model: str | None = None,
    poll_seconds: float = 5.0,
    timeout_seconds: float = 1800.0,
    session: requests.Session | None = None,
    on_note: Any = None,
) -> dict[str, Any]:
    """一次完整跑通（提交 → 轮询 → 拿 url）。主要给面板「测试出片」与探针用。

    真实的 OCV 链路**不走这里** —— OCV 自己管提交与轮询的持久化身份，
    shim 只按它的节奏逐次调用 :func:`create_task` / :func:`fetch_task`。
    """
    note = on_note or (lambda _message: None)
    payload, notes = build_payload(
        prompt=prompt, ratio=ratio, resolution=resolution, duration=duration,
        seed=seed, reference_images=reference_images, model=model,
    )
    for item in notes:
        note(item)
    session = session or requests.Session()
    created = create_task(payload, api_key=api_key, session=session)
    note(f"视频任务已提交：{created['video_id']}（模型 {payload['model']}，模式 {payload['mode']}）")

    deadline = time.monotonic() + float(timeout_seconds)
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = fetch_task(created["video_id"], api_key=api_key, model=payload["model"], session=session)
        if last["status"] in {"SUCCESS", "FAILED", "CANCELLED"}:
            break
        time.sleep(max(0.5, float(poll_seconds)))
    else:
        raise AgnesVideoError(f"Agnes 视频任务 {created['video_id']} 查询超时")

    if last.get("status") != "SUCCESS":
        raise AgnesVideoError(
            f"Agnes 视频任务未能完成（{last.get('status')}）：{last.get('error') or '未提供原因'}"
        )
    if not last.get("url"):
        raise AgnesVideoError("Agnes 视频任务已完成但未返回 url")
    return {"video_id": created["video_id"], "model": payload["model"],
            "mode": payload["mode"], "size": payload["size"],
            "seconds": payload["seconds"], "url": last["url"], "raw": last["raw"]}
