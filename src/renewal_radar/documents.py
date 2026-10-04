from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader


class DocumentError(ValueError):
    pass


def extract_pdf_text(path: Path) -> tuple[str, bool]:
    """Return document text and whether OCR was used for any page."""
    try:
        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as exc:
        raise DocumentError(f"Unable to read PDF: {exc}") from exc

    used_ocr = False
    if any(not page for page in pages):
        try:
            import fitz
            import pytesseract
        except ImportError as exc:
            raise DocumentError(
                "This PDF has no selectable text. Install OCR support with `pip install '.[ocr]'` "
                "and install the Tesseract executable."
            ) from exc
        try:
            pdf = fitz.open(path)
            for index, page_text in enumerate(pages):
                if not page_text:
                    pixmap = pdf[index].get_pixmap(dpi=220)
                    pages[index] = pytesseract.image_to_string(pixmap.pil_image())
                    used_ocr = True
        except Exception as exc:
            raise DocumentError(f"OCR could not read this PDF: {exc}") from exc

    text = "\n\n".join(page for page in pages if page)
    if len(text.strip()) < 20:
        raise DocumentError("The PDF did not contain enough readable text to extract contract terms.")
    return text, used_ocr
