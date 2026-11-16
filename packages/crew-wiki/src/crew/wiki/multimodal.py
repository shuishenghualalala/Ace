"""Wiki 多模态材料理解核心。

为前端上传固定工作流和 Agent 对话式通路提供统一的图片/视频理解能力。
图片理解在进程内直连用户配置的 OpenAI 兼容视觉模型（VLM_* 环境变量）；
视频理解复用 crew/skills/ 下的 video-understanding skill 脚本。
"""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any

import requests

import crew as _crew_pkg
from crew.state.logging import get_logger

log = get_logger("wiki.multimodal")


class MediaUnderstandingError(RuntimeError):
    """媒体理解失败。"""

    def __init__(self, message: str, *, needs_confirmation: bool = False) -> None:
        super().__init__(message)
        self.needs_confirmation = needs_confirmation


# 图片/视频 MIME 前缀
IMAGE_MIME_PREFIXES = ("image/jpeg", "image/png", "image/webp", "image/bmp", "image/gif")
VIDEO_MIME_PREFIXES = ("video/mp4", "video/quicktime", "video/webm", "video/x-msvideo", "video/x-matroska")

# 返回给模型的描述长度上限（头尾保留，中间省略），防超长输出撑爆上下文
MAX_DESCRIPTION_CHARS = 20_000


def _truncate_description(text: str) -> str:
    if len(text) <= MAX_DESCRIPTION_CHARS:
        return text
    keep = MAX_DESCRIPTION_CHARS // 2
    omitted = len(text) - keep * 2
    return f"{text[:keep]}\n…（中间省略 {omitted} 字符）…\n{text[-keep:]}"

# 图片理解的可选格式与体积上限
IMAGE_SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_DEFAULT_IMAGE_PROMPT = "描述一下这张图片"

# 视频 skill 脚本路径。skills 数据归属根发行版 crew（ADR-0037 拆包后与 wiki 不同发行版），
# 顶层 crew 为 PEP 420 命名空间包，遍历其搜索路径 portion 定位 skills/，
# 避免「以 wiki 自身目录回溯包外资源」的文件系统耦合：
# editable 开发态 → 仓库根 crew/skills；site-packages 安装态 → 合并目录；
# PyInstaller 冻结态 → _MEIPASS/crew/skills（--add-data 收录）。
def _crew_skills_dir() -> Path:
    for portion in _crew_pkg.__path__:
        candidate = Path(portion) / "skills"
        if candidate.is_dir():
            return candidate.resolve()
    return (Path(next(iter(_crew_pkg.__path__))) / "skills").resolve()


_VIDEO_UNDERSTAND_SCRIPT = _crew_skills_dir() / "video-understanding" / "scripts" / "video_understand.py"

# 缓存加载的模块
_loaded_modules: dict[str, Any] = {}


def is_image_mime(mime: str) -> bool:
    """判断 MIME 是否为支持的图片类型。"""
    return bool(mime) and any(str(mime).lower().startswith(p) for p in IMAGE_MIME_PREFIXES)


def is_video_mime(mime: str) -> bool:
    """判断 MIME 是否为支持的视频类型。"""
    return bool(mime) and any(str(mime).lower().startswith(p) for p in VIDEO_MIME_PREFIXES)


def is_media_mime(mime: str) -> bool:
    """判断 MIME 是否为支持的图片或视频类型。"""
    return is_image_mime(mime) or is_video_mime(mime)


def _load_script_module(name: str, path: Path) -> Any:
    """通过文件路径动态加载 skill 脚本模块（skill 目录含连字符，无法直接 import）。"""
    if name in _loaded_modules:
        return _loaded_modules[name]

    if not path.exists():
        raise MediaUnderstandingError(f"skill 脚本不存在: {path}")

    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise MediaUnderstandingError(f"无法加载 skill 脚本: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        _loaded_modules[name] = module
        return module
    except SystemExit as exc:
        # skill 脚本 import 失败时可能直接 sys.exit()（如缺依赖），SystemExit 属于
        # BaseException，不拦截会穿透 FastAPI 异常处理打垮整个网关进程，降级为加载失败。
        log.error("skill 脚本加载时异常退出 %s (exit=%s)", path, exc.code)
        raise MediaUnderstandingError(f"skill 脚本加载时异常退出 (exit {exc.code}): {path}") from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("加载 skill 脚本失败 %s", path)
        raise MediaUnderstandingError(f"加载 skill 脚本失败: {exc}") from exc


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


def _vlm_config(key: str) -> str:
    return os.environ.get(key, "").strip() or _env_file_value(key)


def _image_chat_endpoint() -> str:
    base_url = _vlm_config("VLM_BASE_URL").rstrip("/")
    if not base_url:
        return ""
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def _image_timeout_seconds() -> float:
    try:
        return max(5.0, min(300.0, float(_vlm_config("VLM_TIMEOUT_SECONDS") or 120)))
    except ValueError:
        return 120.0


def _classify_vlm_request_error(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.Timeout):
        return f"请求超时（>{_image_timeout_seconds():.0f}s），请检查 VLM_BASE_URL 连通性或调大 VLM_TIMEOUT_SECONDS"
    if isinstance(exc, requests.ConnectionError):
        return "连接失败（DNS 解析失败或服务不可达），请检查 VLM_BASE_URL"
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else "未知"
        return f"HTTP {status}，请检查 VLM_API_KEY 与服务端状态"
    return f"{type(exc).__name__}: {exc}"


def _image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _vision_response_text(payload: Any) -> str:
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


def describe_image(path: str | Path, prompt: str | None = None) -> str:
    """进程内调用配置的 OpenAI 兼容视觉模型，返回图片描述文本。

    Args:
        path: 图片文件路径。
        prompt: 自定义提示词。

    Returns:
        图片描述文本。

    Raises:
        MediaUnderstandingError: 文件校验失败、未配置模型服务或请求失败。
    """
    image_path = Path(path)
    if not image_path.is_file():
        raise MediaUnderstandingError(f"图片文件不存在 - {image_path}")
    if image_path.suffix.lower() not in IMAGE_SUPPORTED_EXTENSIONS:
        raise MediaUnderstandingError(f"不支持的图片格式 - {image_path.suffix.lower()}")
    if image_path.stat().st_size > MAX_IMAGE_BYTES:
        raise MediaUnderstandingError("图片文件超过 10 MB")

    endpoint = _image_chat_endpoint()
    model = _vlm_config("VLM_MODEL")
    if not endpoint or not model:
        raise MediaUnderstandingError("请配置 VLM_BASE_URL 和 VLM_MODEL")

    headers = {"Content-Type": "application/json"}
    api_key = _vlm_config("VLM_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": str(prompt or _DEFAULT_IMAGE_PROMPT)},
                {"type": "image_url", "image_url": {"url": _image_data_url(image_path)}},
            ],
        }],
        "stream": False,
    }
    try:
        response = requests.post(endpoint, headers=headers, json=body, timeout=_image_timeout_seconds())
        response.raise_for_status()
        text = _vision_response_text(response.json())
    except requests.RequestException as exc:
        raise MediaUnderstandingError(f"图片理解请求失败 - {_classify_vlm_request_error(exc)}") from exc
    except (ValueError, OSError) as exc:
        raise MediaUnderstandingError(f"图片理解响应解析失败 - {type(exc).__name__}: {exc}") from exc
    if not text:
        raise MediaUnderstandingError("模型服务未返回图片描述")

    return _truncate_description(text)


def describe_video(path: str | Path, prompt: str | None = None, *, confirm_upload: bool = False) -> str:
    """调用视频理解 skill 返回视频描述文本。

    Args:
        path: 视频文件路径。
        prompt: 自定义提示词。
        confirm_upload: 是否确认上传到外部云端。False 时抛出需要确认的异常。

    Returns:
        视频描述文本。

    Raises:
        MediaUnderstandingError: 理解失败，或 needs_confirmation=True 表示需要用户确认。
    """
    if not confirm_upload:
        raise MediaUnderstandingError(
            "视频理解需要将视频上传到已配置的外部媒体分析服务。"
            "请向用户说明数据外传风险，取得用户明确确认后，再次调用并设置 confirm_upload=true。",
            needs_confirmation=True,
        )

    video_module = _load_script_module("crew_skill_video_understand", _VIDEO_UNDERSTAND_SCRIPT)
    load_api_key = getattr(video_module, "load_api_key")
    upload_video = getattr(video_module, "upload_video")
    analyze_video = getattr(video_module, "analyze_video")

    api_key = load_api_key()
    if not api_key:
        raise MediaUnderstandingError("未找到 VLM_API_KEY，无法分析视频")

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            video_url = upload_video(str(path), api_key)
    except Exception as exc:  # noqa: BLE001
        log.exception("视频上传脚本执行异常")
        raise MediaUnderstandingError(
            f"视频上传脚本异常: {type(exc).__name__}: {exc}"
        ) from exc

    if not video_url:
        output = buf.getvalue().strip()
        raise MediaUnderstandingError(output or "视频上传失败")

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            result = analyze_video(video_url, prompt or "描述下这个视频", api_key)
    except Exception as exc:  # noqa: BLE001
        log.exception("视频分析脚本执行异常")
        raise MediaUnderstandingError(
            f"视频分析脚本异常: {type(exc).__name__}: {exc}"
        ) from exc

    if result is None:
        output = buf.getvalue().strip()
        raise MediaUnderstandingError(output or "视频理解失败，未返回描述")

    return _truncate_description(str(result).strip())


def describe_media(path: str | Path, mime: str, prompt: str | None = None, *, confirm_upload: bool = False) -> str:
    """根据 MIME 类型统一调度图片或视频理解。

    Args:
        path: 媒体文件路径。
        mime: MIME 类型。
        prompt: 自定义提示词。
        confirm_upload: 视频上传确认标志。

    Returns:
        描述文本。

    Raises:
        MediaUnderstandingError: 不支持的类型或理解失败。
    """
    if is_image_mime(mime):
        return describe_image(path, prompt)
    if is_video_mime(mime):
        return describe_video(path, prompt, confirm_upload=confirm_upload)
    raise MediaUnderstandingError(f"不支持的媒体类型: {mime}")
