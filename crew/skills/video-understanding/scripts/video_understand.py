#!/usr/bin/env python3
"""Upload and analyze video with user-configured provider-neutral endpoints."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

DEFAULT_PROMPT = "描述一下这个视频"
SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".flv", ".m4v"}
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_PROMPT_LENGTH = 1000
MAX_OUTPUT_CHARS = 20_000
FORBIDDEN_PATTERNS = (
    "ignore previous", "ignore the above", "system prompt", "developer mode",
    "prompt injection", "leak password", "leak secret", "泄露密钥", "系统提示词",
)


def _emit_error(error: str, message: str) -> None:
    print(
        json.dumps({"status": "error", "error": error, "message": message}, ensure_ascii=False),
        file=sys.stderr,
    )


def _classify_request_error(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.Timeout):
        return f"请求超时（>{_timeout_seconds():.0f}s），请检查 VLM 服务连通性或调大 VLM_TIMEOUT_SECONDS"
    if isinstance(exc, requests.ConnectionError):
        return "连接失败（DNS 解析失败或服务不可达），请检查 VLM 服务地址"
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else "未知"
        return f"HTTP {status}，请检查 VLM_API_KEY 与服务端状态"
    return f"{type(exc).__name__}: {exc}"


def _truncate_output(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    keep = MAX_OUTPUT_CHARS // 2
    omitted = len(text) - keep * 2
    return f"{text[:keep]}\n…（中间省略 {omitted} 字符）…\n{text[-keep:]}"


def _env_file_value(key: str) -> str:
    raw_path = os.environ.get("CREW_ENV_FILE", "").strip()
    if not raw_path:
        return ""
    path = Path(raw_path).expanduser()
    if not path.is_file():
        return ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and stripped.startswith(f"{key}="):
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def _config_value(key: str) -> str:
    return os.environ.get(key, "").strip() or _env_file_value(key)


def load_api_key() -> str:
    return _config_value("VLM_API_KEY")


def _timeout_seconds() -> float:
    try:
        return max(5.0, min(600.0, float(_config_value("VLM_TIMEOUT_SECONDS") or 300)))
    except ValueError:
        return 300.0


def sanitize_prompt(prompt: str | None) -> tuple[str | None, str | None]:
    text = str(prompt or DEFAULT_PROMPT).strip()[:MAX_PROMPT_LENGTH]
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
    text = "".join(
        char for char in text
        if char in {"\n", "\t"} or not unicodedata.category(char).startswith("C")
    )
    lowered = text.lower()
    for pattern in FORBIDDEN_PATTERNS:
        if pattern in lowered:
            return None, "提示词包含不安全的指令覆盖或凭据请求"
    return f"请仅根据视频内容回答以下问题：\n{text}", None


def validate_video(video_path: str | Path) -> bool:
    path = Path(video_path)
    if not path.is_file():
        _emit_error("FileNotFound", f"视频文件不存在 - {path}")
        return False
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        _emit_error("UnsupportedFormat", f"不支持的视频格式 - {path.suffix.lower()}")
        return False
    if path.stat().st_size > MAX_VIDEO_BYTES:
        _emit_error("FileTooLarge", "视频文件超过 100 MB")
        return False
    return True


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _nested_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ("data", "result"):
        if isinstance(payload.get(key), dict):
            return payload[key]
    return payload


def upload_video(video_path: str | Path, api_key: str) -> str | None:
    endpoint = _config_value("VLM_VIDEO_UPLOAD_URL")
    if not endpoint:
        _emit_error("MissingConfig", "请配置 VLM_VIDEO_UPLOAD_URL")
        return None
    if not validate_video(video_path):
        return None
    path = Path(video_path)
    try:
        with path.open("rb") as stream:
            response = requests.post(
                endpoint,
                headers=_headers(api_key),
                files={"file": (path.name, stream)},
                timeout=_timeout_seconds(),
            )
        response.raise_for_status()
        data = _nested_dict(response.json())
    except requests.RequestException as exc:
        _emit_error("UploadFailed", f"视频上传失败 - {_classify_request_error(exc)}")
        return None
    except (ValueError, OSError) as exc:
        _emit_error("ResponseParseFailed", f"视频上传响应解析失败 - {type(exc).__name__}: {exc}")
        return None
    for key in ("fileUrl", "filePath", "url"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    _emit_error("EmptyResponse", "上传接口未返回视频地址")
    return None


def _response_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for container in (payload.get("result"), payload.get("data"), payload):
        if isinstance(container, dict) and isinstance(container.get("text"), str):
            return container["text"].strip()
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"].strip()
    return ""


def analyze_video(video_url: str, prompt: str | None, api_key: str) -> str | None:
    endpoint = _config_value("VLM_VIDEO_ANALYZE_URL")
    model = _config_value("VLM_VIDEO_MODEL")
    if not endpoint or not model:
        _emit_error("MissingConfig", "请配置 VLM_VIDEO_ANALYZE_URL 和 VLM_VIDEO_MODEL")
        return None
    safe_prompt, error = sanitize_prompt(prompt)
    if error or safe_prompt is None:
        _emit_error("UnsafePrompt", error or "提示词校验失败")
        return None
    headers = {**_headers(api_key), "Content-Type": "application/json"}
    body = {"model": model, "prompt": safe_prompt, "video": video_url, "stream": False}
    try:
        response = requests.post(endpoint, headers=headers, json=body, timeout=_timeout_seconds())
        response.raise_for_status()
        text = _response_text(response.json())
    except requests.RequestException as exc:
        _emit_error("AnalyzeFailed", f"视频分析失败 - {_classify_request_error(exc)}")
        return None
    except ValueError as exc:
        _emit_error("ResponseParseFailed", f"视频分析响应解析失败 - {type(exc).__name__}: {exc}")
        return None
    if not text:
        _emit_error("EmptyResponse", "分析接口未返回视频描述")
        return None
    return _truncate_output(text)


def print_security_notice() -> None:
    upload_host = urlparse(_config_value("VLM_VIDEO_UPLOAD_URL")).hostname or "未配置服务"
    print(
        f"安全提示：视频将上传到外部服务 {upload_host}。请确认文件不包含不应外传的数据。",
        file=sys.stderr,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="使用自定义外部服务分析视频")
    parser.add_argument("video_path", help="本地视频路径")
    parser.add_argument("--prompt", "-p", help="针对视频的问题")
    parser.add_argument("--confirm-upload", action="store_true", help="确认同意把视频上传到配置的服务")
    args = parser.parse_args()

    if not args.confirm_upload:
        print_security_notice()
        _emit_error("UploadNotConfirmed", "需要用户明确确认后才能上传视频")
        raise SystemExit(1)
    try:
        api_key = load_api_key()
        video_url = upload_video(args.video_path, api_key)
        if not video_url:
            raise SystemExit(1)
        result = analyze_video(video_url, args.prompt, api_key)
    except SystemExit:
        raise
    except Exception as exc:
        _emit_error("UnexpectedError", f"视频理解脚本异常 - {type(exc).__name__}: {exc}")
        raise SystemExit(1)
    if not result:
        raise SystemExit(1)
    print(_truncate_output(result))


if __name__ == "__main__":
    main()
