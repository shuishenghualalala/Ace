"""Wiki 多模态理解核心单元测试。

图片理解 mock 进程内 requests 调用，视频理解 mock skill 脚本，均不触达真实外部媒体服务。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from crew.wiki import multimodal
from crew.wiki.multimodal import (
    _VIDEO_UNDERSTAND_SCRIPT,
    MediaUnderstandingError,
    _load_script_module,
    describe_image,
    describe_media,
    describe_video,
    is_image_mime,
    is_video_mime,
)

# 共享 venv 的 editable 安装指向主检出时，新 API 可能来自旧代码；
# 装配点新增能力（截断/异常守卫）的用例在旧代码下自动跳过。
MAX_DESCRIPTION_CHARS = getattr(multimodal, "MAX_DESCRIPTION_CHARS", 20000)
requires_assembly_hardening = pytest.mark.skipif(
    not hasattr(multimodal, "_truncate_description"),
    reason="crew.wiki.multimodal 来自未含 H10 加固的检出",
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture
def sample_image(tmp_path: Path) -> Path:
    path = tmp_path / "sample.png"
    path.write_bytes(b"fake png data")
    return path


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    path = tmp_path / "sample.mp4"
    path.write_bytes(b"fake mp4 data")
    return path


def test_is_image_mime():
    assert is_image_mime("image/jpeg") is True
    assert is_image_mime("image/png") is True
    assert is_image_mime("image/webp") is True
    assert is_image_mime("video/mp4") is False
    assert is_image_mime("application/pdf") is False


def test_is_video_mime():
    assert is_video_mime("video/mp4") is True
    assert is_video_mime("video/quicktime") is True
    assert is_video_mime("image/png") is False
    assert is_video_mime("text/plain") is False


def _configure_image_env(monkeypatch):
    monkeypatch.setenv("VLM_BASE_URL", "https://vision.example/v1")
    monkeypatch.setenv("VLM_MODEL", "vision-model")
    monkeypatch.setenv("VLM_API_KEY", "fake-key")
    monkeypatch.delenv("CREW_ENV_FILE", raising=False)


def _mock_image_response(monkeypatch, text: str) -> list:
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse({"choices": [{"message": {"content": text}}]})

    monkeypatch.setattr(multimodal.requests, "post", fake_post)
    return calls


def test_describe_image_returns_text(sample_image: Path, monkeypatch):
    _configure_image_env(monkeypatch)
    calls = _mock_image_response(monkeypatch, "一只猫在睡觉")

    result = describe_image(str(sample_image), "描述图片")

    assert result == "一只猫在睡觉"
    assert calls[0][0] == "https://vision.example/v1/chat/completions"
    body = calls[0][1]["json"]
    assert body["model"] == "vision-model"
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


def test_describe_image_requires_configuration(sample_image: Path, monkeypatch):
    monkeypatch.delenv("VLM_BASE_URL", raising=False)
    monkeypatch.delenv("VLM_MODEL", raising=False)
    monkeypatch.delenv("CREW_ENV_FILE", raising=False)
    post = MagicMock()
    monkeypatch.setattr(multimodal.requests, "post", post)

    with pytest.raises(MediaUnderstandingError):
        describe_image(str(sample_image))
    post.assert_not_called()


def test_describe_video_requires_confirmation(sample_video: Path):
    with pytest.raises(MediaUnderstandingError) as exc_info:
        describe_video(str(sample_video))

    assert exc_info.value.needs_confirmation is True
    assert "确认" in str(exc_info.value)


def test_describe_video_uploads_and_analyzes(sample_video: Path):
    mock_module = MagicMock()
    mock_module.load_api_key.return_value = "fake-key"
    mock_module.upload_video.return_value = "https://media.example/file/123"
    mock_module.analyze_video.return_value = "视频里有一只狗在跑"

    with patch(
        "crew.wiki.multimodal._load_script_module",
        return_value=mock_module,
    ):
        result = describe_video(str(sample_video), "描述视频", confirm_upload=True)

    assert result == "视频里有一只狗在跑"
    mock_module.upload_video.assert_called_once_with(str(sample_video), "fake-key")
    mock_module.analyze_video.assert_called_once_with(
        "https://media.example/file/123", "描述视频", "fake-key"
    )


def test_describe_video_raises_when_upload_fails(sample_video: Path):
    mock_module = MagicMock()
    mock_module.load_api_key.return_value = "fake-key"
    mock_module.upload_video.return_value = None

    with patch(
        "crew.wiki.multimodal._load_script_module",
        return_value=mock_module,
    ), pytest.raises(MediaUnderstandingError):
        describe_video(str(sample_video), confirm_upload=True)


def test_describe_media_routes_to_image(sample_image: Path, monkeypatch):
    _configure_image_env(monkeypatch)
    _mock_image_response(monkeypatch, "image desc")

    result = describe_media(str(sample_image), "image/png")

    assert result == "image desc"


def test_describe_media_routes_to_video(sample_video: Path):
    mock_module = MagicMock()
    mock_module.load_api_key.return_value = "fake-key"
    mock_module.upload_video.return_value = "https://media.example/file/456"
    mock_module.analyze_video.return_value = "video desc"

    with patch(
        "crew.wiki.multimodal._load_script_module",
        return_value=mock_module,
    ):
        result = describe_media(str(sample_video), "video/mp4", confirm_upload=True)

    assert result == "video desc"


def test_describe_media_unsupported_mime():
    with pytest.raises(MediaUnderstandingError) as exc_info:
        describe_media("/tmp/x.txt", "text/plain")

    assert "不支持" in str(exc_info.value)


def test_load_script_module_system_exit(tmp_path: Path):
    """skill 脚本 import 时 sys.exit 不得外溢，应降级为 MediaUnderstandingError。"""
    bad_script = tmp_path / "bad_skill.py"
    bad_script.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")

    with pytest.raises(MediaUnderstandingError) as exc_info:
        _load_script_module("crew_skill_test_bad_exit", bad_script)

    assert "退出" in str(exc_info.value)


def test_bundled_video_skill_uses_only_configured_endpoints(
    sample_video: Path,
    monkeypatch,
):
    module = _load_script_module("crew_skill_video_understand", _VIDEO_UNDERSTAND_SCRIPT)
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if "files" in kwargs:
            return _FakeResponse({"data": {"fileUrl": "https://media.example/video/1"}})
        return _FakeResponse({"choices": [{"message": {"content": "视频描述"}}]})

    monkeypatch.setenv("VLM_VIDEO_UPLOAD_URL", "https://upload.example/media")
    monkeypatch.setenv("VLM_VIDEO_ANALYZE_URL", "https://analyze.example/video")
    monkeypatch.setenv("VLM_VIDEO_MODEL", "video-model")
    monkeypatch.setattr(module.requests, "post", fake_post)

    video_url = module.upload_video(sample_video, "fake-key")
    result = module.analyze_video(video_url, "描述视频", "fake-key")

    assert video_url == "https://media.example/video/1"
    assert result == "视频描述"
    assert [call[0] for call in calls] == [
        "https://upload.example/media",
        "https://analyze.example/video",
    ]


# ── H10：网络错误分类、输出截断、装配点异常守卫 ─────────────────────────────


def _load_video_skill():
    return _load_script_module("crew_skill_video_understand", _VIDEO_UNDERSTAND_SCRIPT)


def test_describe_image_classifies_timeout(sample_image, monkeypatch):
    _configure_image_env(monkeypatch)

    def fake_post(url, **kwargs):
        raise requests.Timeout("slow")

    monkeypatch.setattr(multimodal.requests, "post", fake_post)

    with pytest.raises(MediaUnderstandingError, match="请求超时"):
        describe_image(str(sample_image), "描述")


def test_describe_image_classifies_connection_error(sample_image, monkeypatch):
    _configure_image_env(monkeypatch)

    def fake_post(url, **kwargs):
        raise requests.ConnectionError("dns")

    monkeypatch.setattr(multimodal.requests, "post", fake_post)

    with pytest.raises(MediaUnderstandingError, match="连接失败"):
        describe_image(str(sample_image), "描述")


def test_describe_image_classifies_http_error(sample_image, monkeypatch):
    _configure_image_env(monkeypatch)

    def fake_post(url, **kwargs):
        response = SimpleNamespace(status_code=502)
        raise requests.HTTPError("bad gateway", response=response)

    monkeypatch.setattr(multimodal.requests, "post", fake_post)

    with pytest.raises(MediaUnderstandingError, match="HTTP 502"):
        describe_image(str(sample_image), "描述")


def test_describe_image_raises_on_empty_response(sample_image, monkeypatch):
    _configure_image_env(monkeypatch)
    _mock_image_response(monkeypatch, "")

    with pytest.raises(MediaUnderstandingError, match="未返回图片描述"):
        describe_image(str(sample_image), "描述")


def test_bundled_video_skill_classifies_upload_timeout(sample_video, monkeypatch, capsys):
    module = _load_video_skill()
    monkeypatch.setenv("VLM_VIDEO_UPLOAD_URL", "https://upload.example/media")

    def fake_post(url, **kwargs):
        raise requests.Timeout("slow")

    monkeypatch.setattr(module.requests, "post", fake_post)

    assert module.upload_video(sample_video, "fake-key") is None
    assert "请求超时" in capsys.readouterr().err


@requires_assembly_hardening
def test_describe_image_truncates_long_description(sample_image, monkeypatch):
    _configure_image_env(monkeypatch)
    _mock_image_response(monkeypatch, "字" * (MAX_DESCRIPTION_CHARS * 3))

    result = describe_image(str(sample_image))

    assert len(result) < MAX_DESCRIPTION_CHARS * 2
    assert "省略" in result


@requires_assembly_hardening
def test_describe_video_wraps_upload_exception(sample_video):
    mock_module = MagicMock()
    mock_module.load_api_key.return_value = "fake-key"
    mock_module.upload_video.side_effect = ValueError("bad url")

    with patch(
        "crew.wiki.multimodal._load_script_module",
        return_value=mock_module,
    ), pytest.raises(MediaUnderstandingError) as exc_info:
        describe_video(str(sample_video), confirm_upload=True)

    assert "脚本异常" in str(exc_info.value)
