"""vision_analyze：modality 门控、Pillow 归一化与多模态 media 通路。"""

from __future__ import annotations

import base64
import io
import json

import pytest

from crew.core.errors import ToolError
from crew.core.runctx import current_model_capabilities
from crew.tools import web_tools

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


@pytest.fixture
def authorized_png(tmp_path, monkeypatch):
    png = tmp_path / "authorized.png"
    png.write_bytes(_TINY_PNG)

    async def authorize(args, **kwargs):
        return png

    monkeypatch.setattr(web_tools, "authorize_file_tool", authorize)
    return png


@pytest.mark.asyncio
async def test_rejects_non_vision_model_before_read(authorized_png, monkeypatch):
    called = []

    async def authorize(args, **kwargs):
        called.append(args)
        return authorized_png

    monkeypatch.setattr(web_tools, "authorize_file_tool", authorize)
    token = current_model_capabilities.set(("text", "tools"))
    try:
        with pytest.raises(ToolError, match="不支持视觉"):
            await web_tools.handle_vision_analyze({"path": "x.png"})
    finally:
        current_model_capabilities.reset(token)
    assert called == []


@pytest.mark.asyncio
async def test_allows_vision_model_and_returns_media(authorized_png):
    token = current_model_capabilities.set(("text", "tools", "vision"))
    try:
        output = await web_tools.handle_vision_analyze({"path": "x.png"})
    finally:
        current_model_capabilities.reset(token)

    payload = json.loads(output.content)
    assert payload["image"]["width"] == 1
    assert payload["image"]["resized"] is False
    assert output.media[0].mime_type.startswith("image/")
    assert output.media[0].data_url.startswith("data:image/")
    assert output.media[0].detail == "high"


@pytest.mark.asyncio
async def test_unknown_capabilities_are_not_gated(authorized_png):
    # 无运行时能力上下文（直接调用/单测）不预设拒绝；provider 层仍会把
    # 图片块降级为占位文本，不会打挂纯文本模型请求。
    output = await web_tools.handle_vision_analyze({"path": "x.png"})
    assert len(output.media) == 1


@pytest.mark.asyncio
async def test_unsupported_format_raises(authorized_png):
    authorized_png.write_bytes(b"not an image at all")
    with pytest.raises(ToolError, match="不支持的图片格式"):
        await web_tools.handle_vision_analyze({"path": "x.png"})


PIL = pytest.importorskip("PIL", reason="Pillow 为可选依赖，缺失时跳过归一化测试")
from PIL import Image  # noqa: E402


def _make_jpeg(size: tuple[int, int], *, orientation: int | None = None) -> bytes:
    image = Image.new("RGB", size, (200, 30, 40))
    buffer = io.BytesIO()
    kwargs = {"format": "JPEG"}
    if orientation is not None:
        from PIL import ExifTags

        exif = Image.Exif()
        exif[ExifTags.Base.Orientation] = orientation
        kwargs["exif"] = exif.tobytes()
    image.save(buffer, **kwargs)
    return buffer.getvalue()


def _decode_output_image(output) -> Image.Image:
    data_url = output.media[0].data_url
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return Image.open(io.BytesIO(raw))


@pytest.mark.asyncio
async def test_exif_orientation_is_applied(authorized_png):
    authorized_png.write_bytes(_make_jpeg((2, 1), orientation=6))

    output = await web_tools.handle_vision_analyze({"path": "x.png"})

    # 元信息保留原始尺寸，实际像素已按 EXIF 旋转。
    payload = json.loads(output.content)
    assert (payload["image"]["width"], payload["image"]["height"]) == (2, 1)
    decoded = _decode_output_image(output)
    assert decoded.size == (1, 2)


@pytest.mark.asyncio
async def test_pixel_budget_scales_large_images(authorized_png):
    authorized_png.write_bytes(_make_jpeg((4000, 3000)))

    output = await web_tools.handle_vision_analyze({"path": "x.png"})

    payload = json.loads(output.content)
    assert payload["image"]["resized"] is True
    decoded = _decode_output_image(output)
    assert max(decoded.size) <= web_tools._VISION_MAX_DIMENSION
    assert decoded.size[0] * decoded.size[1] <= web_tools._VISION_MAX_PIXELS


@pytest.mark.asyncio
async def test_exif_metadata_is_stripped(authorized_png):
    authorized_png.write_bytes(_make_jpeg((4, 4), orientation=6))

    output = await web_tools.handle_vision_analyze({"path": "x.png"})

    decoded = _decode_output_image(output)
    assert decoded.getexif().get(274) is None
