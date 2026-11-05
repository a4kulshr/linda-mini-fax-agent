"""Render every PDF page and OCR it. The PDFs are image-only, so text comes from Tesseract."""
from pathlib import Path

import pymupdf
import pytesseract
from PIL import Image

DPI = 300
MIN_CHARS = 40       # below this a page is treated as unreadable
MIN_CONF = 55.0


def validate_pdf(path: Path, expected_pages: int) -> int:
    """Raises if the file is not a complete PDF with the expected page count."""
    head = path.read_bytes()[:5]
    if head != b"%PDF-":
        raise ValueError(f"not a PDF (starts with {head!r})")
    with pymupdf.open(path) as doc:
        if doc.page_count != expected_pages:
            raise ValueError(f"PDF has {doc.page_count} pages, portal lists {expected_pages}")
        for page in doc:
            page.get_pixmap(dpi=30)  # forces a full decode; truncated files fail here
        return doc.page_count


def ocr_pdf(path: Path) -> list[dict]:
    pages = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, 1):
            pix = page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
            img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
            confs = [float(c) for c, w in zip(data["conf"], data["text"]) if w.strip() and float(c) >= 0]
            text = pytesseract.image_to_string(img)
            conf = sum(confs) / len(confs) if confs else 0.0
            chars = len("".join(text.split()))
            pages.append({"page": i, "text": text, "confidence": round(conf, 1),
                          "readable": chars >= MIN_CHARS and conf >= MIN_CONF})
    return pages
