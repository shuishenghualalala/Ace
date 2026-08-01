"""Generate assistant.png using the same frame language as assets/user.png."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

ROOT = Path(__file__).resolve().parents[1]
USER = ROOT / "assets" / "user.png"
ICON = ROOT / "assets" / "icon.png"
OUT = ROOT / "assets" / "assistant.png"

SIZE = 200
PAD = 12
RADIUS = 36


def rounded_square_mask(size: int, pad: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((pad, pad, size - pad - 1, size - pad - 1), radius=radius, fill=255)
    return mask


def glyph_cutout_from_icon(icon: Image.Image, inner_size: int = 132) -> Image.Image:
    """White = cutout area (transparent in final avatar)."""
    rgba = icon.convert("RGBA")
    w, h = rgba.size
    side = min(w, h)
    rgba = rgba.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
    rgba = rgba.resize((inner_size, inner_size), Image.Resampling.LANCZOS)

    gray = ImageOps.grayscale(rgba).convert("L")
    alpha = rgba.split()[-1]
    content = ImageChops.lighter(gray, alpha)
    content = content.point(lambda p: 255 if p > 28 else 0)
    content = content.filter(ImageFilter.MaxFilter(3))
    content = content.filter(ImageFilter.GaussianBlur(1.2))
    content = content.point(lambda p: 255 if p > 48 else 0)

    cutout = Image.new("L", (SIZE, SIZE), 0)
    ox = (SIZE - inner_size) // 2
    oy = (SIZE - inner_size) // 2 + 4
    cutout.paste(content, (ox, oy))
    return cutout


def compose_avatar(frame_mask: Image.Image, cutout_mask: Image.Image) -> Image.Image:
    """Black rounded frame with transparent glyph hole, matching user.png."""
    black = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 255))
    alpha = frame_mask.copy()
    alpha = ImageChops.subtract(alpha, cutout_mask)
    black.putalpha(alpha)
    return black


def main() -> None:
    frame = rounded_square_mask(SIZE, PAD, RADIUS)
    cutout = glyph_cutout_from_icon(Image.open(ICON))
    assistant = compose_avatar(frame, cutout)
    assistant.save(OUT)
    print("wrote", OUT, "bbox", assistant.getbbox())


if __name__ == "__main__":
    main()
