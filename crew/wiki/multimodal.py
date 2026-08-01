"""Wiki 多模态材料理解核心。

为前端上传固定工作流和 Agent 对话式通路提供统一的图片/视频理解能力。
复用 crew/skills/ 下的 image-understanding 与 video-understanding skill 脚本。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from pathlib import Path
from typing import Any

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

# skill 脚本路径
_IMAGE_UNDERSTAND_SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "image-understanding" / "scripts" / "image_understand.py"
_VIDEO_UNDERSTAND_SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "video-understanding" / "scripts" / "video_understand.py"

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


def describe_image(path: str | Path, prompt: str | None = None) -> str:
    """调用图片理解 skill 返回图片描述文本。

    Args:
        path: 图片文件路径。
        prompt: 自定义提示词，缺省使用 skill 默认提示词。

    Returns:
        图片描述文本。

    Raises:
        MediaUnderstandingError: 理解失败。
    """
    image_module = _load_script_module("crew_skill_image_understand", _IMAGE_UNDERSTAND_SCRIPT)
    analyze_image = getattr(image_module, "analyze_image")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        result = analyze_image(str(path), prompt)

    if result is None:
        output = buf.getvalue().strip()
        message = output or "图片理解失败，未返回描述"
        raise MediaUnderstandingError(message)

    return str(result).strip()


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
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        video_url = upload_video(str(path), api_key)

    if not video_url:
        output = buf.getvalue().strip()
        raise MediaUnderstandingError(output or "视频上传失败")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        result = analyze_video(video_url, prompt or "描述下这个视频", api_key)

    if result is None:
        output = buf.getvalue().strip()
        raise MediaUnderstandingError(output or "视频理解失败，未返回描述")

    return str(result).strip()


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
