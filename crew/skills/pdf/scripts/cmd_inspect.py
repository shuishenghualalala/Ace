"""Render every PDF page and report suspiciously sparse layouts."""

from __future__ import annotations

import math
from pathlib import Path


def _white_background(image):
    from PIL import Image

    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, "white")
    background.alpha_composite(rgba)
    return background.convert("RGB")


def _page_metrics(image, white_threshold: int) -> dict:
    grayscale = _white_background(image).convert("L")
    ink_mask = grayscale.point(lambda value: 255 if value < white_threshold else 0)
    histogram = ink_mask.histogram()
    ink_pixels = histogram[255]
    total_pixels = max(1, grayscale.width * grayscale.height)
    bounds = ink_mask.getbbox()
    if bounds is None:
        bbox_width_ratio = 0.0
        bbox_height_ratio = 0.0
        bbox_area_ratio = 0.0
    else:
        left, top, right, bottom = bounds
        bbox_width_ratio = (right - left) / grayscale.width
        bbox_height_ratio = (bottom - top) / grayscale.height
        bbox_area_ratio = ((right - left) * (bottom - top)) / total_pixels
    return {
        "inkRatio": ink_pixels / total_pixels,
        "contentBoundingBox": list(bounds) if bounds is not None else None,
        "contentWidthRatio": bbox_width_ratio,
        "contentHeightRatio": bbox_height_ratio,
        "contentAreaRatio": bbox_area_ratio,
    }


def _contact_sheet(page_images: list[tuple[int, object]], output_path: Path) -> None:
    from PIL import Image, ImageDraw

    if not page_images:
        return
    columns = min(3, len(page_images))
    thumb_width = 360
    thumb_height = 480
    label_height = 28
    padding = 16
    rows = math.ceil(len(page_images) / columns)
    sheet = Image.new(
        "RGB",
        (
            padding + columns * (thumb_width + padding),
            padding + rows * (thumb_height + label_height + padding),
        ),
        "#d9dde3",
    )
    draw = ImageDraw.Draw(sheet)
    resampling = getattr(Image, "Resampling", Image).LANCZOS
    for index, (page_number, image) in enumerate(page_images):
        thumbnail = _white_background(image)
        thumbnail.thumbnail((thumb_width, thumb_height), resampling)
        column = index % columns
        row = index // columns
        x = padding + column * (thumb_width + padding)
        y = padding + row * (thumb_height + label_height + padding)
        tile = Image.new("RGB", (thumb_width, thumb_height), "white")
        tile.paste(
            thumbnail,
            ((thumb_width - thumbnail.width) // 2, (thumb_height - thumbnail.height) // 2),
        )
        sheet.paste(tile, (x, y))
        draw.text((x, y + thumb_height + 6), f"Page {page_number}", fill="black")
    sheet.save(output_path)


def inspect_pdf(
    pdf_path: str,
    output_dir: str,
    *,
    dpi: int = 120,
    min_ink_ratio: float = 0.003,
    min_content_height_ratio: float = 0.08,
    white_threshold: int = 245,
) -> dict:
    """Render all pages and return image paths, metrics, and low-content warnings."""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError(
            "pypdfium2 is required for PDF visual QA; repair the Daimon managed Python runtime"
        ) from exc

    source = Path(pdf_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"PDF file not found: {source}")
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)

    document = pdfium.PdfDocument(str(source))
    rendered_pages: list[tuple[int, object]] = []
    page_reports = []
    warnings = []
    scale = dpi / 72
    try:
        for page_index in range(len(document)):
            page_number = page_index + 1
            page = document[page_index]
            image = page.render(scale=scale).to_pil()
            output_path = destination / f"page-{page_number:03d}.png"
            image.save(output_path)
            metrics = _page_metrics(image, white_threshold)
            page_warnings = []
            if metrics["inkRatio"] < min_ink_ratio:
                page_warnings.append(
                    f"ink ratio {metrics['inkRatio']:.4f} is below {min_ink_ratio:.4f}"
                )
            if metrics["contentHeightRatio"] < min_content_height_ratio:
                page_warnings.append(
                    "content height ratio "
                    f"{metrics['contentHeightRatio']:.4f} is below {min_content_height_ratio:.4f}"
                )
            if page_warnings:
                warnings.append(
                    {
                        "page": page_number,
                        "code": "low_content_page",
                        "message": "; ".join(page_warnings),
                    }
                )
            page_reports.append(
                {
                    "page": page_number,
                    "image": str(output_path),
                    **metrics,
                    "warnings": page_warnings,
                }
            )
            rendered_pages.append((page_number, image.copy()))
            page.close()
    finally:
        document.close()

    contact_sheet_path = destination / "contact-sheet.png"
    _contact_sheet(rendered_pages, contact_sheet_path)
    return {
        "pdf": str(source),
        "outputDir": str(destination),
        "pageCount": len(page_reports),
        "contactSheet": str(contact_sheet_path),
        "pages": page_reports,
        "warnings": warnings,
    }
