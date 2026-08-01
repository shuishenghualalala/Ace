#!/usr/bin/env python3
"""Analyze a local image with a user-configured OpenAI-compatible endpoint."""

from __future__ import annotations

import argparse
import base64
import mimetypes
import os
from pathlib import Path
from typing import Any

import requests

DEFAULT_PROMPT = "描述一下这张图片"
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024


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


def _chat_endpoint() -> str:
    base_url = _config_value("VLM_BASE_URL").rstrip("/")
    if not base_url:
        return ""
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def _timeout_seconds() -> float:
    try:
        return max(5.0, min(300.0, float(_config_value("VLM_TIMEOUT_SECONDS") or 120)))
    except ValueError:
        return 120.0


def validate_image(image_path: str | Path) -> bool:
    path = Path(image_path)
    if not path.is_file():
        print(f"错误：图片文件不存在 - {path}")
        return False
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        print(f"错误：不支持的图片格式 - {path.suffix.lower()}")
        return False
    if path.stat().st_size > MAX_IMAGE_BYTES:
        print("错误：图片文件超过 10 MB")
        return False
    return True


def _image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _response_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "").strip()
            for item in content
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        return "\n".join(parts)
    return ""


def analyze_image(image_path: str | Path, prompt: str | None = None) -> str | None:
    endpoint = _chat_endpoint()
    model = _config_value("VLM_MODEL")
    if not endpoint or not model:
        print("错误：请配置 VLM_BASE_URL 和 VLM_MODEL")
        return None
    if not validate_image(image_path):
        return None

    path = Path(image_path)
    headers = {"Content-Type": "application/json"}
    api_key = load_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": str(prompt or DEFAULT_PROMPT)},
                {"type": "image_url", "image_url": {"url": _image_data_url(path)}},
            ],
        }],
        "stream": False,
    }
    try:
        response = requests.post(endpoint, headers=headers, json=body, timeout=_timeout_seconds())
        response.raise_for_status()
        text = _response_text(response.json())
    except (requests.RequestException, ValueError, OSError) as exc:
        print(f"错误：图片理解请求失败 - {type(exc).__name__}")
        return None
    if not text:
        print("错误：模型服务未返回图片描述")
        return None
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="使用 OpenAI 兼容视觉模型分析图片")
    parser.add_argument("image_path", help="本地图片路径")
    parser.add_argument("--prompt", "-p", help="针对图片的问题")
    args = parser.parse_args()
    result = analyze_image(args.image_path, args.prompt)
    if not result:
        raise SystemExit(1)
    print(result)


if __name__ == "__main__":
    main()
