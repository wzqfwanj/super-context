#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""超级上下文 · 媒体分类与描述生成流水线。

接口见 docs/CONTRACT.md 第 3 节：
    KINDS / classify / guess_mime / Settings.from_env / describe_file

设计约束：
- 只用 Python 标准库；外部能力通过 subprocess 调用 ffmpeg / ffprobe /
  pdftotext / pdftoppm（poppler-utils）。
- describe_file 不向外抛普通异常：任何失败都转成 {"error": 中文原因} +
  文件名兜底 description，content 可为空字符串。
- 模型调用走百炼 OpenAI 兼容接口，单次调用有超时（CONTEXT_TIMEOUT，默认 120s）
  且失败自动重试一次。
- 临时文件集中在 tempfile.TemporaryDirectory 中，退出即清理。
- 日志写 stderr，带时间戳；绝不输出 API Key。
"""

from __future__ import annotations

import base64
import glob
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass

KINDS = ("image", "document", "audio", "video", "other")

# 百炼 OpenAI 兼容地址（标准 DashScope）。Token Plan / 专属工作空间用户通过
# CONTEXT_BASE_URL 覆盖为控制台给出的专属 base_url。
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
DEFAULT_VISION_MODEL = "qwen3-vl-plus"
DEFAULT_TEXT_MODEL = "qwen-plus"
DEFAULT_ASR_MODEL = "qwen3-asr-flash"
DEFAULT_MAX_TEXT_CHARS = 30000
DEFAULT_TIMEOUT = 120

CHAT_PATH = "/compatible-mode/v1/chat/completions"

# 图片直接透传的 MIME；其余格式先转码
_IMAGE_PASSTHROUGH_MIME = ("image/jpeg", "image/png", "image/webp")
_IMAGE_MAX_BYTES = 8 * 1024 * 1024

# 描述长度上限（契约）
_DESC_LIMITS = {
    "image": 250,
    "document": 120,
    "audio": 120,
    "video": 300,
    "other": 300,
}

# ASR 分段与总时长上限
_ASR_SEGMENT_SECONDS = 240
_ASR_TOTAL_CAP_SECONDS = 30 * 60

# 视频均匀抽帧数量
_VIDEO_FRAMES = 6

_KIND_ZH = {
    "image": "图片",
    "document": "文档",
    "audio": "音频",
    "video": "视频",
    "other": "文件",
}

_EXT_KIND = {}
for _ext in (
    "png jpg jpeg gif webp bmp tif tiff svg ico heic heif avif jfif".split()
):
    _EXT_KIND[_ext] = "image"
for _ext in (
    "txt md markdown json jsonl csv tsv log xml html htm yaml yml ini cfg "
    "conf toml rst tex srt vtt pdf docx pptx xlsx doc ppt xls rtf epub "
    "py js ts tsx jsx java c h cpp cc hpp go rs sql sh bash bat ps1 "
    "css scss less vue php rb kt swift".split()
):
    _EXT_KIND[_ext] = "document"
for _ext in "mp3 wav m4a aac flac ogg opus wma amr aiff aif ape ac3".split():
    _EXT_KIND[_ext] = "audio"
for _ext in (
    "mp4 mov avi mkv webm flv wmv m4v 3gp mpeg mpg ts ogv m2ts mts".split()
):
    _EXT_KIND[_ext] = "video"

_PLAIN_TEXT_EXT = set(
    (
        "txt md markdown json jsonl csv tsv log xml html htm yaml yml ini cfg "
        "conf toml rst tex srt vtt py js ts tsx jsx java c h cpp cc hpp go rs "
        "sql sh bash bat ps1 css scss less vue php rb kt swift"
    ).split()
)
_LEGACY_OFFICE_EXT = {"doc", "ppt", "xls"}

# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


def _log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        print("[%s] [describe] %s" % (stamp, message), file=sys.stderr, flush=True)
    except Exception:  # 日志本身绝不能影响主流程
        pass


class _ModelError(Exception):
    """内部异常：message 一定是中文可读原因，describe_file 会兜住。"""


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _log("环境变量 %s=%r 不是整数，使用默认值 %s" % (name, raw, default))
        return default
    if value <= 0:
        _log("环境变量 %s=%r 必须为正整数，使用默认值 %s" % (name, raw, default))
        return default
    return value


def _clip(text: str, limit: int) -> str:
    """截断到 limit 个字符；超长时补省略号（总长不超过 limit）。"""
    text = (text or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _size_human(num_bytes: int) -> str:
    size = float(max(0, int(num_bytes or 0)))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return "%d B" % int(size)
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f GB" % size


def _fmt_duration(seconds: float) -> str:
    total = int(max(0, round(float(seconds or 0))))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%02d:%02d" % (minutes, secs)


def _fallback_description(name: str, kind: str, size: int) -> str:
    return "《%s》（%s，%s）" % (
        name or "未命名文件",
        _KIND_ZH.get(kind, "文件"),
        _size_human(size),
    )


# ---------------------------------------------------------------------------
# 契约第 3 节：分类与 MIME
# ---------------------------------------------------------------------------


def classify(name: str) -> str:
    """按扩展名分类；未知扩展名 -> "other"。"""
    ext = os.path.splitext(str(name or ""))[1].lower().lstrip(".")
    return _EXT_KIND.get(ext, "other")


def guess_mime(name: str) -> str:
    """复用标准库 mimetypes；未知 -> application/octet-stream。"""
    try:
        mime, _encoding = mimetypes.guess_type(str(name or ""))
    except Exception:
        mime = None
    return mime or "application/octet-stream"


@dataclass
class Settings:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    vision_model: str = DEFAULT_VISION_MODEL
    text_model: str = DEFAULT_TEXT_MODEL
    asr_model: str = DEFAULT_ASR_MODEL
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS
    timeout: int = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_key=(os.environ.get("CONTEXT_API_KEY") or "").strip(),
            base_url=(
                (os.environ.get("CONTEXT_BASE_URL") or "").strip() or DEFAULT_BASE_URL
            ).rstrip("/"),
            vision_model=(
                (os.environ.get("CONTEXT_VISION_MODEL") or "").strip()
                or DEFAULT_VISION_MODEL
            ),
            text_model=(
                (os.environ.get("CONTEXT_TEXT_MODEL") or "").strip()
                or DEFAULT_TEXT_MODEL
            ),
            asr_model=(
                (os.environ.get("CONTEXT_ASR_MODEL") or "").strip()
                or DEFAULT_ASR_MODEL
            ),
            max_text_chars=_env_int("CONTEXT_MAX_TEXT_CHARS", DEFAULT_MAX_TEXT_CHARS),
            timeout=_env_int("CONTEXT_TIMEOUT", DEFAULT_TIMEOUT),
        )


# ---------------------------------------------------------------------------
# 百炼 OpenAI 兼容接口
# ---------------------------------------------------------------------------


def _endpoint(settings: Settings) -> str:
    return settings.base_url.rstrip("/") + CHAT_PATH


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        payload = json.loads(body)
    except Exception:
        return body.strip()[:300]
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or ""
            if msg:
                return str(msg)[:300]
        for key in ("message", "error_msg", "code"):
            val = payload.get(key)
            if isinstance(val, (str, int)):
                return str(val)[:300]
    return body.strip()[:300]


def _http_error_message(code: int, detail: str) -> str:
    if code in (401, 403):
        return "百炼鉴权失败（HTTP %d），请检查 CONTEXT_API_KEY 是否正确" % code
    if code == 429:
        return "百炼接口限流或额度不足（HTTP 429），请稍后重试"
    if code >= 500:
        return "百炼服务暂时不可用（HTTP %d）" % code
    hint = ("：%s" % detail) if detail else ""
    return "百炼接口返回 HTTP %d%s" % (code, hint)


def _chat(settings: Settings, payload: dict, timeout: int = None) -> dict:
    """带一次重试的 chat/completions 调用，返回解析后的 JSON。"""
    url = _endpoint(settings)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    call_timeout = int(timeout or settings.timeout or DEFAULT_TIMEOUT)
    attempts = 2
    last_error = "模型调用失败"
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % settings.api_key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=call_timeout) as response:
                raw = response.read()
            try:
                return json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                raise _ModelError("百炼接口返回了无法解析的响应（非 JSON）")
        except _ModelError:
            raise
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            message = _http_error_message(exc.code, detail)
            # 4xx（鉴权/参数/内容问题）重试无意义，直接失败
            if 400 <= exc.code < 500 and exc.code != 429:
                raise _ModelError(message)
            last_error = message
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            last_error = "网络请求失败：%s" % (reason,)
        if attempt < attempts:
            _log("模型调用第 %d 次失败（%s），1.5s 后重试" % (attempt, last_error))
            time.sleep(1.5)
    raise _ModelError(last_error)


def _message_text(data: dict) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices or not isinstance(choices, list):
        raise _ModelError("百炼接口未返回 choices（响应异常）")
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                value = part.get("text")
                if isinstance(value, str):
                    parts.append(value)
        text = "".join(parts).strip()
    else:
        text = ""
    if not text:
        raise _ModelError("百炼模型未返回文本内容")
    return text


def _chat_text(settings: Settings, prompt: str, timeout: int = None) -> str:
    payload = {
        "model": settings.text_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }
    return _message_text(_chat(settings, payload, timeout))


def _chat_vision(settings: Settings, prompt: str, image_urls: list) -> str:
    content = [{"type": "text", "text": prompt}]
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    payload = {
        "model": settings.vision_model,
        "messages": [{"role": "user", "content": content}],
    }
    return _message_text(_chat(settings, payload))


def _chat_asr(settings: Settings, wav_path: str) -> str:
    try:
        with open(wav_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise _ModelError("读取待转写音频失败：%s" % (exc,))
    if not raw:
        raise _ModelError("待转写音频为空")
    encoded = base64.b64encode(raw).decode("ascii")
    payload = {
        "model": settings.asr_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": "data:audio/wav;base64,%s" % encoded,
                            "format": "wav",
                        },
                    }
                ],
            }
        ],
        "modalities": ["text"],
    }
    return _message_text(_chat(settings, payload))


def _summarize(settings: Settings, text: str, limit: int, subject: str) -> str:
    prompt = (
        "请用简体中文为下面的%s写一段不超过 %d 字的概括，"
        "只输出概括正文，不要标题、不要列表、不要 Markdown、不要引号。\n\n%s"
        % (subject, limit, _clip(text, 8000))
    )
    return _chat_text(settings, prompt)


# ---------------------------------------------------------------------------
# 外部命令
# ---------------------------------------------------------------------------


def _require_tools(*names):
    missing = [name for name in names if not shutil.which(name)]
    if missing:
        raise _ModelError(
            "缺少外部命令：%s（音视频处理需要 ffmpeg/ffprobe，PDF 需要 poppler-utils）"
            % "、".join(missing)
        )


def _run(cmd, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(5, int(timeout)),
        )
    except subprocess.TimeoutExpired:
        raise _ModelError("外部命令执行超时（%s 秒）：%s" % (timeout, os.path.basename(cmd[0])))
    except OSError as exc:
        raise _ModelError("无法执行外部命令 %s：%s" % (os.path.basename(cmd[0]), exc))


def _run_text(cmd, timeout: int) -> str:
    result = _run(cmd, timeout)
    return (result.stdout or b"").decode("utf-8", "replace")


def _stderr_snippet(result) -> str:
    text = (result.stderr or b"").decode("utf-8", "replace").strip()
    return text[-300:] if text else ""


def _file_data_url(path: str, mime: str) -> str:
    with open(path, "rb") as handle:
        raw = handle.read()
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


def _probe_media(path: str) -> dict:
    """ffprobe 读取时长与是否含音轨。"""
    _require_tools("ffprobe")
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type",
        "-of",
        "json",
        path,
    ]
    result = _run(cmd, 60)
    if result.returncode != 0:
        detail = _stderr_snippet(result)
        raise _ModelError("无法读取媒体信息（文件可能损坏或格式不受支持）%s" % (("：" + detail) if detail else ""))
    try:
        payload = json.loads((result.stdout or b"").decode("utf-8", "replace") or "{}")
    except ValueError:
        raise _ModelError("无法解析 ffprobe 输出（文件可能损坏）")
    duration = 0.0
    fmt = payload.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    streams = payload.get("streams") or []
    has_audio = any(
        isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams
    )
    return {"duration": duration, "has_audio": has_audio}


def _wav_duration(path: str) -> float:
    try:
        with wave.open(path, "rb") as handle:
            rate = handle.getframerate() or 1
            return float(handle.getnframes()) / float(rate)
    except Exception:
        return 0.0


def _to_wav(path: str, temp_dir: str, settings: Settings) -> str:
    """转成 16k 单声道 wav。"""
    _require_tools("ffmpeg")
    out = os.path.join(temp_dir, "audio_16k.wav")
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        out,
    ]
    result = _run(cmd, settings.timeout)
    if (
        result.returncode != 0
        or not os.path.exists(out)
        or os.path.getsize(out) == 0
    ):
        detail = _stderr_snippet(result)
        raise _ModelError(
            "音轨解码失败（文件可能损坏或格式不受支持）%s"
            % (("：" + detail) if detail else "")
        )
    return out


def _cut_segment(wav_path: str, start: float, seconds: float, temp_dir: str, index: int, settings: Settings) -> str:
    out = os.path.join(temp_dir, "asr_seg_%02d.wav" % index)
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-ss",
        "%.3f" % start,
        "-t",
        "%.3f" % seconds,
        "-i",
        wav_path,
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        out,
    ]
    result = _run(cmd, settings.timeout)
    if result.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        detail = _stderr_snippet(result)
        raise _ModelError("音频分段失败%s" % (("：" + detail) if detail else ""))
    return out


def _transcribe_long(
    settings: Settings,
    wav_path: str,
    temp_dir: str,
    duration: float,
    subject: str = "音频",
) -> tuple:
    """对 wav 做（可能的）分段 ASR，返回 (转写全文, 说明文本列表)。"""
    total = duration if duration and duration > 0 else _wav_duration(wav_path)
    notes = []
    limit = total
    if total > _ASR_TOTAL_CAP_SECONDS:
        limit = float(_ASR_TOTAL_CAP_SECONDS)
        notes.append(
            "（%s总时长 %s，超过 30 分钟，仅转写前 30 分钟）"
            % (subject, _fmt_duration(total))
        )

    segments = []  # [(start, seconds)]
    if total <= 0:
        segments = [(0.0, 0.0)]  # 时长未知，整段转写
        notes.append("（未能读取%s时长，按整段转写）" % subject)
    elif limit <= _ASR_SEGMENT_SECONDS:
        segments = [(0.0, limit)]
    else:
        start = 0.0
        while start < limit - 0.5 and len(segments) < 40:
            seconds = min(float(_ASR_SEGMENT_SECONDS), limit - start)
            if seconds < 1.0:
                break
            segments.append((start, seconds))
            start += seconds

    parts = []
    failures = []
    for index, (start, seconds) in enumerate(segments):
        if len(segments) == 1 and total <= 0:
            segment_path = wav_path
        elif len(segments) == 1 and seconds >= total - 0.5:
            segment_path = wav_path
        else:
            try:
                segment_path = _cut_segment(
                    wav_path, start, seconds, temp_dir, index, settings
                )
            except _ModelError as exc:
                failures.append(str(exc))
                continue
        try:
            text = _chat_asr(settings, segment_path)
        except _ModelError as exc:
            failures.append(str(exc))
            continue
        text = text.strip()
        if text:
            parts.append(text)
        _log("ASR 段 %d/%d 完成（%s）" % (index + 1, len(segments), _fmt_duration(seconds)))

    if not parts:
        raise _ModelError("语音转写失败：%s" % ("；".join(failures[:2]) or "未识别到内容"))
    if failures:
        notes.append("（有 %d 段语音转写失败）" % len(failures))
    return "\n".join(parts), notes


# ---------------------------------------------------------------------------
# 图片 / 视频帧
# ---------------------------------------------------------------------------


def _jpeg_scale(side: int) -> str:
    return "scale=w='min(%d,iw)':h='min(%d,ih)':force_original_aspect_ratio=decrease" % (
        side,
        side,
    )


def _image_data_url(path: str, name: str, temp_dir: str, settings: Settings) -> str:
    size = os.path.getsize(path)
    mime = guess_mime(name)
    if size <= _IMAGE_MAX_BYTES and mime in _IMAGE_PASSTHROUGH_MIME:
        return _file_data_url(path, mime)

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        if size <= _IMAGE_MAX_BYTES and mime.startswith("image/"):
            _log("缺少 ffmpeg，按原格式透传图片（%s）" % mime)
            return _file_data_url(path, mime)
        raise _ModelError(
            "图片需要转码（超过 8MB 或格式不受支持），但未找到 ffmpeg"
        )
    out = os.path.join(temp_dir, "image_1600.jpg")
    cmd = [
        ffmpeg,
        "-v",
        "error",
        "-y",
        "-i",
        path,
        "-frames:v",
        "1",
        "-vf",
        _jpeg_scale(1600),
        "-q:v",
        "3",
        out,
    ]
    result = _run(cmd, settings.timeout)
    if result.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        detail = _stderr_snippet(result)
        raise _ModelError(
            "图片解码失败（文件可能损坏或格式不受支持）%s"
            % (("：" + detail) if detail else "")
        )
    try:
        return _file_data_url(out, "image/jpeg")
    except OSError as exc:
        raise _ModelError("读取压缩后的图片失败：%s" % (exc,))


_IMAGE_PROMPT = (
    "请用简体中文描述这张图片：主体/人物/物体、场景环境、"
    "图中可见文字（如能辨认请照抄原文，不要臆造）、整体风格与可能用途。"
    "不超过250字；只输出描述正文，不要标题、不要列表、不要 Markdown。"
)

_VIDEO_PROMPT = (
    "这些是从同一段视频中按时间顺序均匀抽取的画面帧。请用简体中文综合描述视频内容："
    "画面主体与场景、关键动作或变化、可见文字（照抄原文）、整体风格与可能用途。"
    "不超过250字；只输出描述正文，不要标题、不要列表、不要 Markdown。"
)

_PDF_OCR_PROMPT = (
    "以下是同一份 PDF 文档的前几页图像。请提取并整理其中的文字内容："
    "按页、按段落顺序输出原文，保留标题与层级；如果是扫描件请逐字识别。"
    "只输出提取到的文字，不要额外说明、不要 Markdown 代码块。"
)


def _extract_frames(
    path: str, duration: float, temp_dir: str, settings: Settings
) -> list:
    _require_tools("ffmpeg")
    if not duration or duration <= 0:
        raise _ModelError("无法读取视频时长（文件可能损坏）")
    scale = _jpeg_scale(960)
    frames = []
    for index in range(_VIDEO_FRAMES):
        # 均匀取区间中点，避免首尾黑场/片尾
        timestamp = duration * (index + 0.5) / _VIDEO_FRAMES
        out = os.path.join(temp_dir, "frame_%02d.jpg" % index)
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            "%.3f" % timestamp,
            "-i",
            path,
            "-frames:v",
            "1",
            "-vf",
            scale,
            "-q:v",
            "3",
            out,
        ]
        result = _run(cmd, min(settings.timeout, 120))
        if result.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            try:
                frames.append(_file_data_url(out, "image/jpeg"))
            except OSError:
                continue
    if not frames:
        raise _ModelError("视频抽帧失败（文件可能损坏或没有可用画面）")
    return frames


# ---------------------------------------------------------------------------
# 文档文本提取
# ---------------------------------------------------------------------------


def _decode_text(raw: bytes) -> tuple:
    """utf-8（含 BOM）→ gbk/gb18030 → utf-16（有 BOM）→ utf-8 replace。"""
    if not raw:
        return "", "utf-8"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8-sig", "gbk", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8(replace)"


def _looks_binary(raw: bytes) -> bool:
    if not raw:
        return False
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    sample = raw[:8192]
    if b"\x00" in sample:
        return True
    printable = sum(
        1 for byte in sample if byte in (9, 10, 13) or 32 <= byte < 127 or byte >= 128
    )
    return (printable / float(len(sample))) < 0.85


def _read_bytes_limited(path: str, limit: int) -> bytes:
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    read_size = max(4096, int(limit))
    with open(path, "rb") as handle:
        return handle.read(read_size if size > read_size else size + 1)


def _extract_plain(path: str, limit: int) -> str:
    raw = _read_bytes_limited(path, max(limit * 4, 64 * 1024))
    if _looks_binary(raw):
        raise _ModelError("文件看起来是二进制内容，无法按文本读取")
    text, encoding = _decode_text(raw)
    _log("纯文本解码使用 %s" % encoding)
    return text


def _extract_docx(path: str, limit: int) -> str:
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            target = "word/document.xml"
            if target not in names:
                raise _ModelError("docx 中未找到 word/document.xml（文件可能损坏）")
            xml_bytes = archive.read(target)
    except zipfile.BadZipFile:
        raise _ModelError("无法读取 docx（文件已损坏或不是有效的 docx）")
    except KeyError:
        raise _ModelError("无法读取 docx（文件结构异常）")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise _ModelError("docx 正文 XML 解析失败（文件可能损坏）")
    lines = []
    for paragraph in root.iter(namespace + "p"):
        texts = [node.text or "" for node in paragraph.iter(namespace + "t")]
        line = "".join(texts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _extract_pptx(path: str, limit: int) -> str:
    slide_pattern = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
    try:
        with zipfile.ZipFile(path) as archive:
            slide_names = []
            for name in archive.namelist():
                match = slide_pattern.match(name)
                if match:
                    slide_names.append((int(match.group(1)), name))
            if not slide_names:
                raise _ModelError("pptx 中未找到幻灯片（文件可能损坏）")
            slide_names.sort()
            blocks = []
            for order, (number, member) in enumerate(slide_names, start=1):
                xml_bytes = archive.read(member)
                try:
                    root = ET.fromstring(xml_bytes)
                except ET.ParseError:
                    continue
                texts = [
                    node.text
                    for node in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t")
                    if node.text
                ]
                body = "\n".join(text.strip() for text in texts if text.strip())
                if body:
                    blocks.append("【第%d页】\n%s" % (order, body))
                if sum(len(block) for block in blocks) > limit * 2:
                    break
    except zipfile.BadZipFile:
        raise _ModelError("无法读取 pptx（文件已损坏或不是有效的 pptx）")
    return "\n\n".join(blocks)


def _extract_xlsx(path: str, limit: int) -> str:
    sheet_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            shared = []
            if "xl/sharedStrings.xml" in names:
                try:
                    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                    for item in root.iter(sheet_ns + "si"):
                        text = "".join(
                            (node.text or "")
                            for node in item.iter(sheet_ns + "t")
                        )
                        shared.append(text)
                except ET.ParseError:
                    shared = []
            worksheet_names = sorted(
                name
                for name in names
                if re.match(r"^xl/worksheets/sheet\d+\.xml$", name)
            )
            if not worksheet_names:
                raise _ModelError("xlsx 中未找到工作表（文件可能损坏）")
            sheet_titles = []
            if "xl/workbook.xml" in names:
                try:
                    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
                    for sheet in workbook.iter(sheet_ns + "sheet"):
                        sheet_titles.append(sheet.get("name") or "")
                except ET.ParseError:
                    sheet_titles = []

            blocks = []
            total_chars = 0
            for index, member in enumerate(worksheet_names):
                try:
                    root = ET.fromstring(archive.read(member))
                except ET.ParseError:
                    continue
                rows = []
                for row in root.iter(sheet_ns + "row"):
                    cells = []
                    for cell in row.findall(sheet_ns + "c"):
                        cell_type = cell.get("t") or ""
                        value = ""
                        if cell_type == "s":
                            holder = cell.find(sheet_ns + "v")
                            if holder is not None and holder.text:
                                try:
                                    value = shared[int(holder.text)]
                                except (ValueError, IndexError):
                                    value = holder.text
                        elif cell_type == "inlineStr":
                            holder = cell.find(sheet_ns + "is")
                            if holder is not None:
                                value = "".join(
                                    (node.text or "")
                                    for node in holder.iter(sheet_ns + "t")
                                )
                        else:
                            holder = cell.find(sheet_ns + "v")
                            if holder is not None and holder.text:
                                value = holder.text
                        cells.append(value.strip())
                    if any(cells):
                        rows.append("\t".join(cells))
                if not rows:
                    continue
                title = (
                    sheet_titles[index]
                    if index < len(sheet_titles) and sheet_titles[index]
                    else "Sheet%d" % (index + 1)
                )
                block = "【工作表：%s】\n%s" % (title, "\n".join(rows))
                blocks.append(block)
                total_chars += len(block)
                if total_chars > limit * 2:
                    break
    except zipfile.BadZipFile:
        raise _ModelError("无法读取 xlsx（文件已损坏或不是有效的 xlsx）")
    return "\n\n".join(blocks)


def _extract_pdf(path: str, temp_dir: str, settings: Settings) -> tuple:
    """返回 (文本, 提取方式)。提取方式为 "text" 或 "ocr"。"""
    tried = []
    pdftotext = shutil.which("pdftotext")
    if pdftotext:
        result = _run(
            [pdftotext, "-layout", "-enc", "UTF-8", path, "-"], settings.timeout
        )
        if result.returncode == 0:
            text = (result.stdout or b"").decode("utf-8", "replace").strip()
            if text:
                _log("PDF 文本提取成功（pdftotext -layout，%d 字）" % len(text))
                return text, "text"
            tried.append("pdftotext 未提取到文本（可能是扫描件）")
        else:
            detail = _stderr_snippet(result)
            tried.append(
                "pdftotext 解析失败%s" % (("：" + detail) if detail else "")
            )
    else:
        tried.append("缺少 pdftotext")

    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm:
        prefix = os.path.join(temp_dir, "pdf_page")
        result = _run(
            [pdftoppm, "-png", "-r", "150", "-f", "1", "-l", "3", path, prefix],
            settings.timeout,
        )
        pages = sorted(glob.glob(prefix + "*.png"))
        if result.returncode != 0 or not pages:
            detail = _stderr_snippet(result)
            tried.append(
                "pdftoppm 渲染失败%s" % (("：" + detail) if detail else "")
            )
        else:
            image_urls = []
            for page in pages[:3]:
                try:
                    image_urls.append(_file_data_url(page, "image/png"))
                except OSError:
                    continue
            if image_urls:
                text = _chat_vision(settings, _PDF_OCR_PROMPT, image_urls)
                _log("PDF 按扫描件处理：渲染 %d 页交视觉模型识别" % len(image_urls))
                return text, "ocr"
            tried.append("PDF 页面图片读取失败")
    else:
        tried.append("缺少 pdftoppm")

    if not pdftotext and not pdftoppm:
        raise _ModelError(
            "PDF 文本提取不可用（缺少 pdftotext 与 pdftoppm，请安装 poppler-utils）"
        )
    raise _ModelError("PDF 文本提取失败：%s" % "；".join(tried))


def _extract_document(path: str, name: str, temp_dir: str, settings: Settings) -> tuple:
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    limit = max(1000, int(settings.max_text_chars))
    if ext == "pdf":
        return _extract_pdf(path, temp_dir, settings)
    if ext == "docx":
        return _extract_docx(path, limit), "text"
    if ext == "pptx":
        return _extract_pptx(path, limit), "text"
    if ext == "xlsx":
        return _extract_xlsx(path, limit), "text"
    if ext in _LEGACY_OFFICE_EXT:
        raise _ModelError(
            "不支持旧版 Office 二进制格式（.%s），请另存为 .%sx 后重试"
            % (ext, ext)
        )
    if ext in _PLAIN_TEXT_EXT or ext in ("", "text", "log"):
        return _extract_plain(path, limit), "text"
    # 其他文档扩展名：先按文本尝试一次
    try:
        return _extract_plain(path, limit), "text"
    except _ModelError:
        raise _ModelError("暂不支持该文档格式（.%s），且无法按纯文本读取" % (ext or "无扩展名"))


# ---------------------------------------------------------------------------
# 各类媒体的描述流程
# ---------------------------------------------------------------------------


def _describe_image(path: str, name: str, settings: Settings, size: int) -> tuple:
    with tempfile.TemporaryDirectory(prefix="superctx-") as temp_dir:
        data_url = _image_data_url(path, name, temp_dir, settings)
    description = _chat_vision(settings, _IMAGE_PROMPT, [data_url])
    if not description:
        raise _ModelError("视觉模型未返回图片描述")
    return _clip(description, _DESC_LIMITS["image"]), "", ""


def _describe_document(path: str, name: str, settings: Settings, size: int) -> tuple:
    with tempfile.TemporaryDirectory(prefix="superctx-") as temp_dir:
        text, method = _extract_document(path, name, temp_dir, settings)
    text = (text or "").strip()
    if not text:
        raise _ModelError("未能从文档中提取到文本内容（可能是扫描件或空文件）")
    content = _clip(text, settings.max_text_chars)
    if method == "ocr":
        content = _clip(
            content
            + "\n\n（说明：该 PDF 为扫描件或文本层不可用，以上正文由视觉模型识别）",
            settings.max_text_chars,
        )
    try:
        description = _summarize(
            settings, content, _DESC_LIMITS["document"], "文档正文"
        )
    except _ModelError as exc:
        return (
            _fallback_description(name, "document", size),
            content,
            "生成文档摘要失败：%s" % (exc,),
        )
    return _clip(description, _DESC_LIMITS["document"]), content, ""


def _describe_audio(path: str, name: str, settings: Settings, size: int) -> tuple:
    _require_tools("ffprobe", "ffmpeg")
    info = _probe_media(path)
    with tempfile.TemporaryDirectory(prefix="superctx-") as temp_dir:
        wav_path = _to_wav(path, temp_dir, settings)
        transcript, notes = _transcribe_long(
            settings, wav_path, temp_dir, info.get("duration") or 0.0, subject="音频"
        )
    transcript = (transcript or "").strip()
    if not transcript:
        raise _ModelError("未识别到语音内容（音频可能是纯音乐、静音或无人声）")
    content = ("\n".join(notes) + "\n" + transcript) if notes else transcript
    content = _clip(content, settings.max_text_chars)
    try:
        description = _summarize(
            settings, transcript, _DESC_LIMITS["audio"], "音频转写内容"
        )
    except _ModelError as exc:
        return (
            _fallback_description(name, "audio", size),
            content,
            "生成音频摘要失败：%s" % (exc,),
        )
    return _clip(description, _DESC_LIMITS["audio"]), content, ""


def _describe_video(path: str, name: str, settings: Settings, size: int) -> tuple:
    _require_tools("ffprobe", "ffmpeg")
    info = _probe_media(path)
    duration = info.get("duration") or 0.0
    errors = []
    notes = []
    visual = ""
    transcript = ""
    with tempfile.TemporaryDirectory(prefix="superctx-") as temp_dir:
        frames = []
        try:
            frames = _extract_frames(path, duration, temp_dir, settings)
        except _ModelError as exc:
            errors.append("画面抽帧失败：%s" % (exc,))
        if frames:
            try:
                visual = _chat_vision(settings, _VIDEO_PROMPT, frames).strip()
            except _ModelError as exc:
                errors.append("画面描述失败：%s" % (exc,))
        if info.get("has_audio"):
            try:
                wav_path = _to_wav(path, temp_dir, settings)
                transcript, audio_notes = _transcribe_long(
                    settings,
                    wav_path,
                    temp_dir,
                    duration,
                    subject="音轨",
                )
                transcript = (transcript or "").strip()
                notes.extend(audio_notes)
            except _ModelError as exc:
                errors.append("语音转写失败：%s" % (exc,))
        else:
            _log("视频无音轨，只生成画面描述")

    if not visual and not transcript:
        raise _ModelError("视频处理失败：%s" % ("；".join(errors) or "无可用内容"))

    content = "【画面描述】" + (visual or "（未能生成画面描述）")
    if transcript:
        content += "\n\n【语音转写】" + (("\n".join(notes) + "\n") if notes else "") + transcript
    elif info.get("has_audio"):
        content += "\n\n【语音转写】（未能转写）"
    content = _clip(content, settings.max_text_chars)

    if visual and transcript:
        prompt = (
            "下面是同一段视频的画面描述和语音转写。请用简体中文写一段不超过 %d 字的"
            "内容概述，说明视频讲了什么、拍了什么、有哪些关键信息。"
            "只输出概述正文，不要标题、不要列表、不要 Markdown。\n\n"
            "【画面描述】\n%s\n\n【语音转写】\n%s"
            % (
                _DESC_LIMITS["video"],
                _clip(visual, 4000),
                _clip(transcript, 6000),
            )
        )
        try:
            description = _chat_text(settings, prompt)
        except _ModelError as exc:
            errors.append("生成综合描述失败：%s" % (exc,))
            description = "%s；语音内容：%s" % (
                _clip(visual, 160),
                _clip(transcript, 120),
            )
    elif visual:
        description = visual
    else:
        try:
            description = _summarize(
                settings, transcript, _DESC_LIMITS["video"], "视频语音转写"
            )
        except _ModelError as exc:
            errors.append("生成视频描述失败：%s" % (exc,))
            description = _clip(transcript, _DESC_LIMITS["video"])
    return (
        _clip(description, _DESC_LIMITS["video"]),
        content,
        "；".join(errors),
    )


def _describe_other(path: str, name: str, settings: Settings, size: int) -> tuple:
    content = ""
    try:
        raw = _read_bytes_limited(path, max(settings.max_text_chars * 4, 64 * 1024))
        if raw and not _looks_binary(raw):
            text, encoding = _decode_text(raw)
            _log("其他类型按文本尝试解码：%s" % encoding)
            content = _clip(text, settings.max_text_chars)
    except OSError as exc:
        _log("其他类型读取失败（忽略，仅用文件名兜底）：%s" % (exc,))
    return _fallback_description(name, "other", size), content, ""


_HANDLERS = {
    "image": _describe_image,
    "document": _describe_document,
    "audio": _describe_audio,
    "video": _describe_video,
    "other": _describe_other,
}


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------


def describe_file(path: str, name: str, settings: Settings = None) -> dict:
    """阻塞生成描述，返回 {"kind","mime","description","content","error"}。

    绝不向外抛普通异常；失败时 error 为中文可读原因，description 为文件名兜底。
    """
    settings = settings or Settings.from_env()
    display_name = os.path.basename(str(name or "").strip()) or os.path.basename(
        str(path or "")
    )
    kind = classify(display_name)
    result = {
        "kind": kind,
        "mime": guess_mime(display_name),
        "description": "",
        "content": "",
        "error": "",
    }
    started = time.time()
    size = 0
    try:
        size = os.path.getsize(path)
        if size <= 0:
            raise _ModelError("文件为空（0 字节）")
        if not settings.api_key:
            result["error"] = "未配置百炼 API Key（CONTEXT_API_KEY）"
            result["description"] = _fallback_description(display_name, kind, size)
            _log("未配置 API Key，返回兜底描述：%s" % display_name)
            return result

        handler = _HANDLERS.get(kind, _describe_other)
        description, content, partial_error = handler(path, display_name, settings, size)
        result["description"] = _clip(description, _DESC_LIMITS.get(kind, 300))
        result["content"] = _clip(content, settings.max_text_chars)
        result["error"] = partial_error or ""
    except _ModelError as exc:
        if not size:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        result["error"] = str(exc)
        result["description"] = _fallback_description(display_name, kind, size)
        _log("处理失败（%s）：%s" % (display_name, exc))
    except Exception as exc:  # 兜住所有普通异常，绝不外抛
        if not size:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        result["error"] = "处理文件时出现未预期错误：%s" % (exc,)
        result["description"] = _fallback_description(display_name, kind, size)
        import traceback

        _log(
            "未预期异常（%s）：%s\n%s"
            % (display_name, exc, traceback.format_exc(limit=3))
        )
    elapsed = time.time() - started
    _log(
        "完成 %s（%s，%s）耗时 %.1fs，error=%r"
        % (display_name, kind, _size_human(size), elapsed, result["error"])
    )
    return result


# ---------------------------------------------------------------------------
# 命令行入口：python describe.py <文件路径> [显示名]
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python describe.py <文件路径> [显示名]", file=sys.stderr)
        raise SystemExit(2)
    _path = sys.argv[1]
    if len(sys.argv) > 2:
        _name = sys.argv[2]
    else:
        _name = os.path.basename(_path)
    _settings = Settings.from_env()
    _result = describe_file(_path, _name, _settings)
    print(json.dumps(_result, ensure_ascii=False, indent=2))
