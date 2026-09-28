"""商汤 SenseNova 图片生成客户端。

这份实现只在本地 shim 进程内被调用：shim 对外讲 RunningHub 的
「提交 → 轮询」异步协议，对内把请求翻译成商汤的**同步**接口。

协议映射要点：

===============  ==========================================================
OCV（RunningHub）  商汤 SenseNova
===============  ==========================================================
``/text-to-image``  ``POST /v1/images/generations``
``/image-to-image`` ``POST /v1/images/edits``
``imageUrls``（字符串数组） ``images``（``{"image_url": ...}`` 对象数组）
``aspectRatio`` + ``resolution`` ``size: "2720x1536"``（具体像素）
异步 ``taskId`` + 轮询  同步返回 ``data[0]``
===============  ==========================================================

三个必须显式下发的参数（商汤默认值会破坏 OCV 的约束）：

* ``watermark=False``     —— 默认 true，会打上日日新水印
* ``prompt_extend=False`` —— 默认 true，会自动扩写提示词，破坏 OCV 精心
  锁死的单镜头 / 画风 / 角色一致性约束
* ``response_format=b64_json`` —— ``url`` 模式返回的是 24 小时失效的临时链接
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import requests

from . import config


class SenseImageError(RuntimeError):
    pass


# 长边基准像素。商汤要求宽高均为 32 的倍数、范围 512–4096、最大比例 3:1。
_LONG_SIDE = {"1k": 1280, "2k": 2720, "4k": 4096}
_MAX_RATIO = 3.0


def _parse_ratio(ratio: str) -> tuple[float, float]:
    raw = str(ratio or "").strip().replace("：", ":").replace("x", ":").replace("*", ":")
    if ":" in raw:
        left, _, right = raw.partition(":")
        try:
            width, height = float(left), float(right)
            if width > 0 and height > 0:
                return width, height
        except ValueError:
            pass
    return 16.0, 9.0


def _snap(value: float) -> int:
    """对齐到 32 的倍数并夹紧到商汤允许的区间。

    显式用 ``int(x + 0.5)`` 而不是 ``round()``：后者是 banker's rounding，
    在 720/32 = 22.5 这种正好落在中间的情况下会向下取整成 704，
    让 16:9 偏成 1.82（正确的邻居 736 是 1.74，明显更接近 16:9）。
    """
    stepped = int(value / 32.0 + 0.5) * 32
    return max(512, min(4096, stepped))


def size_for(ratio: str, resolution: str) -> str:
    """把 OCV 的 ``aspectRatio`` + ``resolution`` 换算成商汤的 ``size``。"""
    width_ratio, height_ratio = _parse_ratio(ratio)
    if width_ratio / height_ratio > _MAX_RATIO:
        width_ratio = _MAX_RATIO * height_ratio
    elif height_ratio / width_ratio > _MAX_RATIO:
        height_ratio = _MAX_RATIO * width_ratio

    long_side = _LONG_SIDE.get(str(resolution or "").strip().lower(), _LONG_SIDE["1k"])
    if width_ratio >= height_ratio:
        width = long_side
        height = long_side * height_ratio / width_ratio
    else:
        height = long_side
        width = long_side * width_ratio / height_ratio
    return f"{_snap(width)}x{_snap(height)}"


# 图片魔数 -> MIME。SNIFF 只认**字节**，不认扩展名。
_MAGIC_TABLE: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def sniff_image_mime(blob: bytes) -> str:
    """按文件**内容**判断图片类型；认不出来返回空串。

    这是本项目踩过的一个真实故障（2026-09-23，107 次出图失败）的修复基础，
    详见 ``docs/DEVLOG.md``：

    商汤对 ``images[].image_url`` 的校验以**内容**为准，声明不符会直接
    ``HTTP 400 ... data URL media type does not match the image content``；
    而 OCV 是用 ``mimetypes.guess_type(文件名)``（**只看扩展名**）拼 data URI 的。
    历史项目里存在扩展名 ``.jpg``、内容却是 PNG 的参考图（实测 5371 张里 12 张），
    两者一对不上就确定性失败 —— 与 Key / 额度 / 限流完全无关。
    """
    if not blob:
        return ""
    for prefix, mime in _MAGIC_TABLE:
        if blob.startswith(prefix):
            return mime
    # RIFF....WEBP
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    # TIFF：小端 II*\0 / 大端 MM\0*
    if blob[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    # ISO-BMFF 家族：avif / heic
    if len(blob) >= 12 and blob[4:8] == b"ftyp":
        brand = blob[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"mif1"):
            return "image/heic"
    return ""


def _mime_from_content_type(raw: str) -> str:
    """``image/jpeg; charset=UTF-8`` -> ``image/jpeg``。"""
    return str(raw or "").split(";", 1)[0].strip().lower()


def _correct_data_uri(value: str) -> str:
    """纠正 data URI 头部声明的 media type，使其与真实字节一致。

    **这是本插件最关键的图片兼容修复。** OCV 的 ``_reference_image_url()``
    在上传端点不可用时回退为 ``data:{mimetypes.guess_type(路径)};base64,...``，
    也就是**按扩展名**声明类型。参考图历史包袱里存在 ``.jpg`` 实为 PNG 的文件
    （由早期版本写盘时造成），于是声明 ``image/jpeg``、内容却是 PNG 头 ——
    商汤按内容校验，必然 400。此处在**不改动负载**的前提下把头部换成嗅探值，
    代价只需一次 base64 解码（本地内存操作）。

    嗅探不出来时原样返回：宁可保持上游语义，也不猜。
    """
    header, separator, payload = value.partition(",")
    if not separator or not payload or ";base64" not in header.lower():
        return value
    try:
        blob = base64.b64decode(payload, validate=False)
    except (ValueError, TypeError):
        return value
    sniffed = sniff_image_mime(blob)
    if not sniffed:
        return value
    declared = _mime_from_content_type(header[len("data:"):])
    if declared == sniffed:
        return value
    return f"data:{sniffed};base64,{payload}"


def _to_data_uri(image: str) -> str:
    """把参考图统一成商汤接受的形态。

    商汤只接受**公网可访问**的 http(s) URL 或 ``data:image/*;base64,`` 形式；
    不接受无前缀的裸 base64。OCV 本地传进来的 http URL 指向 127.0.0.1，
    商汤无从访问，因此这里会把本地回环地址的图片读回并转成 data URI。

    两条分支都必须让 **media type 与真实字节一致**：
    data URI 走 :func:`_correct_data_uri` 纠正上游的扩展名声明；
    回环地址下载走 :func:`sniff_image_mime` 嗅探，而不是信任响应头。
    """
    value = str(image or "").strip()
    if not value:
        return ""
    if value.startswith("data:"):
        return _correct_data_uri(value) if config.image_sniff_mime() else value
    if value.startswith("http://127.0.0.1") or value.startswith("http://localhost"):
        try:
            fetched = requests.get(value, timeout=30)
            fetched.raise_for_status()
        except requests.RequestException:
            return ""
        blob = fetched.content
        # 优先按字节嗅探：本地静态服务可能用扩展名给出错误的 Content-Type。
        mime = ""
        if config.image_sniff_mime():
            mime = sniff_image_mime(blob)
        if not mime:
            mime = _mime_from_content_type(fetched.headers.get("Content-Type") or "")
        if not mime:
            mime = "image/jpeg"
        encoded = base64.b64encode(blob).decode("ascii")
        return f"data:{mime};base64,{encoded}"
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return ""


def _normalize_references(images: list[str] | None) -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    for item in images or []:
        uri = _to_data_uri(item)
        if uri:
            references.append({"image_url": uri})
    limit = config.image_max_reference()
    # 商汤官方调优建议：多参考图控制在 2–3 张，过多会稀释主体。
    return references[:limit]


def _extract_image(payload: dict[str, Any]) -> bytes:
    items = payload.get("data")
    if not isinstance(items, list) or not items:
        raise SenseImageError(f"商汤返回中没有图片数据: {json.dumps(payload, ensure_ascii=False)[:400]}")
    first = items[0]
    if not isinstance(first, dict):
        raise SenseImageError("商汤返回的 data[0] 结构不可识别")
    encoded = str(first.get("b64_json") or "").strip()
    if encoded:
        if encoded.startswith("data:"):
            _, _, encoded = encoded.partition(",")
        try:
            return base64.b64decode(encoded)
        except (ValueError, TypeError) as exc:
            raise SenseImageError("商汤返回的 b64_json 无法解码") from exc
    url = str(first.get("url") or "").strip()
    if url:
        fetched = requests.get(url, timeout=config.image_timeout())
        fetched.raise_for_status()
        return fetched.content
    raise SenseImageError("商汤返回中没有 b64_json 也没有 url")


def _error_text(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:300].strip()}"
    if not isinstance(payload, dict):
        return f"HTTP {response.status_code}"
    error = payload.get("error")
    if isinstance(error, dict):
        return (
            f"HTTP {response.status_code} "
            f"{error.get('code') or ''} {error.get('message') or ''}".strip()
        )
    return (
        f"HTTP {response.status_code} "
        f"{payload.get('code') or ''} {payload.get('message') or payload.get('detail') or ''}".strip()
    )


def generate(
    *,
    prompt: str,
    ratio: str = "16:9",
    resolution: str = "1k",
    reference_images: list[str] | None = None,
    retries: int = 2,
) -> tuple[bytes, str]:
    """生成一张图片，返回 ``(图片字节, 使用的尺寸)``。"""
    api_key = config.sensenova_image_api_key()
    if not api_key:
        raise SenseImageError("未配置 SENSENOVA_API_KEY；无法调用商汤图片接口")

    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise SenseImageError("商汤图片接口收到空提示词")

    references = _normalize_references(reference_images)
    if references:
        endpoint = f"{config.sensenova_image_base_url()}/images/edits"
        model = config.sensenova_image_edit_model()
        payload: dict[str, Any] = {"model": model, "prompt": clean_prompt, "images": references}
    else:
        endpoint = f"{config.sensenova_image_base_url()}/images/generations"
        model = config.sensenova_image_model()
        payload = {"model": model, "prompt": clean_prompt}

    size = size_for(ratio, resolution)
    payload.update(
        {
            "n": 1,
            "size": size,
            "watermark": config.image_watermark(),
            "prompt_extend": config.image_prompt_extend(),
            "response_format": config.image_response_format(),
        }
    )

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    last_error = ""
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=(20.0, config.image_timeout()),
            )
        except requests.RequestException as exc:
            last_error = f"网络异常 {type(exc).__name__}: {exc}"
            if attempt < attempts:
                time.sleep(min(20.0, 3.0 * attempt))
                continue
            break

        if response.status_code >= 400:
            last_error = _error_text(response)
            if response.status_code in {408, 409, 429} or response.status_code >= 500:
                if attempt < attempts:
                    time.sleep(min(20.0, 3.0 * attempt))
                    continue
            break

        try:
            body = response.json()
        except ValueError:
            last_error = "商汤图片接口返回了非 JSON 内容"
            if attempt < attempts:
                time.sleep(min(20.0, 3.0 * attempt))
                continue
            break
        if not isinstance(body, dict):
            last_error = "商汤图片接口返回结构不可识别"
            break
        try:
            return _extract_image(body), size
        except SenseImageError as exc:
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(min(20.0, 3.0 * attempt))
                continue
            break

    raise SenseImageError(f"商汤图片生成失败（{endpoint}，size={size}）：{last_error[:500]}")


def save_image(blob: bytes, destination: Path, media_type: str = "image/jpeg") -> Path:
    """把图片落盘。装了 Pillow 就统一转成 JPEG，否则原样写出。"""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(blob)) as image:
            image.convert("RGB").save(destination, format="JPEG", quality=95)
        return destination
    except Exception:
        # 没有 Pillow 或图片格式特殊时退回原样落盘；下游 ffmpeg 靠内容嗅探识别。
        destination.write_bytes(blob)
        return destination
