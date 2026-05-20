"""PDF → page PNG images at 200 DPI for Claude Vision."""

from __future__ import annotations

import logging
from pathlib import Path

from app.services.steno.config import MAX_PAGES_PER_PDF, STENO_RENDERS_DIR


logger = logging.getLogger(__name__)


def render_pdf_pages(pdf_path: Path) -> tuple[list[Path], bool]:
    """Returns (image_paths, was_truncated). Caches per pdf_path.stem."""
    import fitz  # noqa: WPS433 — PyMuPDF, lazy-imported so tests can run without it

    out_dir = STENO_RENDERS_DIR / pdf_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    # If renders already exist, reuse them (PDFs are immutable per filename).
    existing = sorted(out_dir.glob("page_*.png"))
    if existing:
        return existing, False

    doc = fitz.open(pdf_path)
    total = len(doc)
    truncated = total > MAX_PAGES_PER_PDF
    n = min(total, MAX_PAGES_PER_PDF)
    image_paths: list[Path] = []
    for i in range(n):
        img_path = out_dir / f"page_{i + 1:03d}.png"
        pix = doc[i].get_pixmap(dpi=200)
        pix.save(str(img_path))
        image_paths.append(img_path)
    doc.close()
    logger.info("Rendered %d pages from %s", len(image_paths), pdf_path.name)
    return image_paths, truncated
