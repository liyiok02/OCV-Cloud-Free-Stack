"""MiMo（小米）语音合成适配器。

刻意只依赖 ``requests``，和 OCV 自带的 ``qwen_tts.py`` 保持同一形态，
这样它可以原地替换 ``backend.app.qwen_tts.synthesize_to_file``，
而 ``module1_agent_director.py`` 一行都不用改。

接口形态（官方文档）：走 ``/v1/chat/completions``，音频从
``choices[0].message.audio.data`` 取 base64，**不是**专用 TTS 端点。

两个容易踩的坑：

1. 要朗读的正文必须放在 ``assistant`` 角色里，风格指令放在 ``user`` 角色里。
   位置写反会得到完全错误的韵律。
2. 官方 cURL 示例用 ``api-key:`` 头，官方 Python SDK 用
   ``Authorization: Bearer``。两个都是官方文档原文，所以这里**同时发送**，
   避免 401 排查。
"""

from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path

import requests

from . import config


DEFAULT_VOICE = "冰糖"

# 官方预置音色（中文）。voiceclone / voicedesign 需要额外的参考音频或描述，
# 不适合作为长视频的主路径（每片请求都要重传参考音频）。
PRESET_VOICES: tuple[str, ...] = ("冰糖", "茉莉", "苏打", "白桦")
PRESET_VOICES_EN: tuple[str, ...] = ("Mia", "Chloe", "Milo", "Dean")


class MimoTtsError(RuntimeError):
    """不带 API Key 的安全错误信息。"""


def _qwen_error_type() -> type[BaseException]:
    """复用 OCV 的 ``QwenTtsError``，让 module1 的 ``except`` 能捕获。

    ``module1_agent_director`` 用 ``from backend.app.qwen_tts import QwenTtsError``
    绑定了这个类对象；只要抛出的是同一个类（或它的子类），except 就成立。
    """
    module = sys.modules.get("backend.app.qwen_tts")
    candidate = getattr(module, "QwenTtsError", None) if module is not None else None
    if isinstance(candidate, type) and issubclass(candidate, BaseException):
        return candidate
    return MimoTtsError


def _fail(message: str) -> None:
    """按 OCV 期望的异常类型抛出。"""
    raise _qwen_error_type()(message)


def resolve_voice(voice: str) -> str:
    """把 OCV 前端写死的 Qwen 音色名映射到 MiMo 音色名。

    前端音色下拉是在 ``useWorkspace.js`` 里硬编码的 Qwen 音色列表，
    插件不改前端，因此在这里做一次映射：
    已经是 MiMo 音色的原样通过，其余按用户配置的映射表回退到默认音色。
    """
    name = str(voice or "").strip()
    if not name:
        return config.mimo_default_voice()
    if name in PRESET_VOICES or name in PRESET_VOICES_EN:
        return name
    if name in config.clone_voices():
        # 克隆音色名是一等目标，直接放行（synthesize 阶段再内联参考音频）
        return name
    mapped = config.voice_map().get(name)
    if mapped:
        return mapped
    return config.mimo_default_voice()


def _headers(api_key: str) -> dict[str, str]:
    """同时发送两种官方鉴权头，规避文档口径不一致。"""
    return {
        "Content-Type": "application/json",
        "api-key": api_key,
        "Authorization": f"Bearer {api_key}",
    }


def _error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:300].strip()}"
    if not isinstance(payload, dict):
        return f"HTTP {response.status_code}"
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        message = str(error.get("message") or "")
        return f"HTTP {response.status_code} {code} {message}".strip()
    code = str(payload.get("code") or "")
    message = str(payload.get("message") or payload.get("detail") or "")
    return f"HTTP {response.status_code} {code} {message}".strip()


def _decode_audio(raw: str) -> bytes:
    data = str(raw or "").strip()
    if not data:
        return b""
    if data.startswith("data:"):
        _, _, data = data.partition(",")
    try:
        return base64.b64decode(data, validate=False)
    except (ValueError, TypeError):
        return b""


def synthesize_to_file(
    *,
    text: str,
    destination: Path,
    instructions: str = "",
    voice: str = DEFAULT_VOICE,
    language_type: str | None = None,
    optimize_instructions: bool = False,
    retries: int = 3,
) -> None:
    """合成一句文案并落盘。

    签名与 ``backend.app.qwen_tts.synthesize_to_file`` 完全一致，
    因此可以直接替换。``language_type`` 被接受但忽略：MiMo 自行处理语种。
    """
    api_key = config.mimo_api_key()
    if not api_key:
        _fail(
            "未配置 MiMo API Key。请在 OCV 界面右下角「云端免费栈」按钮里填写，"
            f"或直接打开 {config.shim_base_url()}/panel"
        )

    clean_text = str(text or "").strip()
    if not clean_text:
        _fail("MiMo-TTS 收到空文本")

    selected_voice = resolve_voice(voice)
    style = str(instructions or "").strip()
    model = config.mimo_model()

    # 克隆音色：注册表命中即切换 voiceclone 模型，参考音频按
    # 官方格式内联为 audio.voice 的 data URI（每次请求都要带上）。
    clone_meta = config.clone_voices().get(selected_voice)
    if clone_meta is not None:
        data_uri = config.clone_audio_data_uri(selected_voice)
        if not data_uri:
            _fail(
                f"克隆音色「{selected_voice}」的参考音频文件已丢失，"
                "请到面板重新上传"
            )
        model = "mimo-v2.5-tts-voiceclone"
        selected_voice = data_uri  # type: ignore[assignment]

    messages: list[dict[str, str]] = []
    if style:
        # 风格指令必须放 user 角色
        messages.append({"role": "user", "content": style})
    # 待朗读正文必须放 assistant 角色
    messages.append({"role": "assistant", "content": clean_text})

    audio_options: dict[str, object] = {"format": "wav", "voice": selected_voice}
    if model.endswith("-voicedesign") and optimize_instructions:
        audio_options["optimize_text_preview"] = True

    payload = {"model": model, "messages": messages, "audio": audio_options}
    url = f"{config.mimo_base_url()}/chat/completions"
    timeout = config.mimo_timeout()
    attempts = max(1, int(retries) or config.mimo_retries())
    last_error = ""

    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                url, headers=_headers(api_key), json=payload, timeout=timeout
            )
        except requests.RequestException as exc:
            last_error = f"网络异常 {type(exc).__name__}: {exc}"
            if attempt < attempts:
                time.sleep(min(20.0, 2.0 * attempt))
                continue
            break

        if response.status_code >= 400:
            last_error = _error_message(response)
            if response.status_code in {408, 409, 429} or response.status_code >= 500:
                if attempt < attempts:
                    time.sleep(min(20.0, 2.0 * attempt))
                    continue
            _fail(f"MiMo-TTS 调用失败：{last_error}")
        try:
            result = response.json()
        except ValueError:
            last_error = f"HTTP {response.status_code} 返回了非 JSON 内容"
            if attempt < attempts:
                time.sleep(min(20.0, 2.0 * attempt))
                continue
            break

        audio = None
        try:
            choices = result.get("choices") or []
            message = choices[0].get("message") or {}
            audio = message.get("audio")
        except (AttributeError, IndexError, TypeError):
            audio = None

        blob = b""
        if isinstance(audio, dict):
            blob = _decode_audio(audio.get("data"))
            if not blob:
                audio_url = str(audio.get("url") or "").strip()
                if audio_url:
                    try:
                        download = requests.get(audio_url, timeout=timeout)
                        download.raise_for_status()
                        blob = download.content
                    except requests.RequestException as exc:
                        last_error = f"音频下载失败 {type(exc).__name__}"
                        blob = b""
        if not blob:
            message_text = ""
            if isinstance(result, dict):
                message_text = str(result.get("message") or result.get("code") or "")
            last_error = last_error or message_text or "响应中没有可用的音频数据"
            if attempt < attempts:
                time.sleep(min(20.0, 2.0 * attempt))
                continue
            break

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # 原子落盘：避免中断时留下半截文件被后续 ffprobe 当成有效产物
        temporary = destination.with_name(destination.name + ".part")
        temporary.write_bytes(blob)
        if temporary.stat().st_size < 128:
            temporary.unlink(missing_ok=True)
            _fail("MiMo-TTS 返回的音频为空")
        os.replace(temporary, destination)
        return

    _fail(f"MiMo-TTS 请求失败（已重试 {attempts} 次）：{last_error[:500]}")
