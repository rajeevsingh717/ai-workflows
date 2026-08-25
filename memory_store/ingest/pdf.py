"""PDF text extraction helpers."""

from pathlib import Path


def extract_pdf_text(path: str) -> str:
    import pdfplumber

    pages: list[str] = []
    with pdfplumber.open(Path(path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                pages.append(text.strip())
    return "\n\n".join(pages)


def is_scanned(text: str, minimum_characters: int = 80) -> bool:
    """Treat a nearly empty extraction as a scanned/image-only PDF."""
    return len("".join(text.split())) < minimum_characters
