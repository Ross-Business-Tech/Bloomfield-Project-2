"""
ocr.py - turns any file into plain text, with "[PAGE n]" markers between pages.

In plain English:
  * PDFs that contain real text are read directly (pdfplumber).
  * PDF pages with little or no text are scans, so we "photograph" the page and read it with
    OCR (optical character recognition). Images (photos, scans) are read the same way.
  * Web pages (HTML) are cleaned: scripts, menus and footers are thrown away.
  * Text-like files (txt, csv, json, xml) are read as they are.

OCR needs a program called Tesseract on your computer ("brew install tesseract poppler").
If it is missing, you get a clear message, not a crash. The OCR engine is swappable: write
another class like TesseractEngine (for example one that calls a cloud document AI service)
and add it to OCR_ENGINES, then set ocr.engine in reader.yaml.

get_text() also reports what share of pages needed OCR, so poor scans can lower confidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

from collectors.common.log import get_logger
from llm.settings import load_reader_config

log = get_logger("ocr")

TEXT_LIKE_TYPES = {"txt", "csv", "json", "xml"}
IMAGE_TYPES = {"image", "png", "jpg", "jpeg", "tif", "tiff", "gif"}
SUPPORTED_TYPES = TEXT_LIKE_TYPES | IMAGE_TYPES | {"pdf", "html"}


class OcrUnavailableError(RuntimeError):
    """The OCR program is not installed (or not set up), so scanned pages cannot be read."""


class UnsupportedFileError(RuntimeError):
    """We do not know how to read this kind of file."""


@dataclass
class TextResult:
    text: str                          # all the text, with [PAGE n] markers
    page_count: int
    ocr_pages: int                     # how many pages had to be read with OCR
    warnings: list[str] = field(default_factory=list)

    @property
    def ocr_share(self) -> float:
        return self.ocr_pages / self.page_count if self.page_count else 0.0


# ---------------------------------------------------------------------------
# OCR engines (swappable)
# ---------------------------------------------------------------------------
class TesseractEngine:
    """Reads text from a picture using the free Tesseract program."""

    def __init__(self, language: str = "eng"):
        self.language = language

    def ocr_image(self, image) -> str:
        try:
            import pytesseract
            pytesseract.get_tesseract_version()          # fails fast if the program is missing
        except Exception as e:                           # ImportError or TesseractNotFoundError
            raise OcrUnavailableError(
                "Cannot read scanned pages: Tesseract OCR is not installed. "
                "On a Mac run:  brew install tesseract poppler   (then pip install -r requirements.txt)."
            ) from e
        return pytesseract.image_to_string(image, lang=self.language)


# engine name (reader.yaml: ocr.engine) -> a class taking language=... with an ocr_image(image) -> str method
OCR_ENGINES = {"tesseract": TesseractEngine}


def get_engine(cfg: dict | None = None):
    cfg = cfg or load_reader_config()
    ocr_cfg = cfg.get("ocr") or {}
    name = ocr_cfg.get("engine", "tesseract")
    if name not in OCR_ENGINES:
        raise ValueError(f"Unknown OCR engine '{name}'. Choose one of: {', '.join(OCR_ENGINES)}")
    return OCR_ENGINES[name](language=ocr_cfg.get("language", "eng"))   # engines take a `language` option


# ---------------------------------------------------------------------------
# Readers for each file type
# ---------------------------------------------------------------------------
def _tidy(text: str) -> str:
    """Trim trailing spaces and squeeze runs of blank lines."""
    text = "\n".join(line.rstrip() for line in (text or "").splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _pdf_text(path: Path, engine, ocr_cfg: dict) -> TextResult:
    import pdfplumber

    min_chars = int(ocr_cfg.get("min_chars_per_page", 40))
    dpi = int(ocr_cfg.get("dpi", 200))
    pages_text, ocr_pages, warnings = [], 0, []
    with pdfplumber.open(str(path)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            text = _tidy(page.extract_text() or "")
            if len(text) < min_chars:                       # a scan (or a blank page): use OCR
                ocr_pages += 1
                text = _tidy(_ocr_pdf_page(path, number, engine, dpi))
            pages_text.append(f"[PAGE {number}]\n{text}")
    return TextResult("\n\n".join(pages_text), len(pages_text), ocr_pages, warnings)


def _ocr_pdf_page(path: Path, page_number: int, engine, dpi: int) -> str:
    try:
        from pdf2image import convert_from_path
        images = convert_from_path(str(path), dpi=dpi, first_page=page_number, last_page=page_number)
    except Exception as e:
        raise OcrUnavailableError(
            "Cannot turn a scanned PDF page into a picture: poppler is not installed. "
            "On a Mac run:  brew install tesseract poppler") from e
    return engine.ocr_image(images[0]) if images else ""


def _image_text(path: Path, engine) -> TextResult:
    from PIL import Image
    with Image.open(path) as img:
        img.load()
        text = _tidy(engine.ocr_image(img))
    return TextResult(f"[PAGE 1]\n{text}", 1, 1)


NOISE_TAGS = ["script", "style", "noscript", "nav", "footer", "aside", "form", "iframe", "svg", "template"]


def _html_text(raw: bytes) -> TextResult:
    soup = BeautifulSoup(raw, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"})
    description = (meta.get("content") or "").strip() if meta else ""
    for tag in soup(NOISE_TAGS):
        tag.decompose()
    body = soup.body or soup
    body_text = _tidy(body.get_text("\n", strip=True))
    parts = [p for p in (title and f"PAGE TITLE: {title}", description and f"PAGE DESCRIPTION: {description}",
                         body_text) if p]
    text = "\n".join(parts)
    warnings = []
    if len(body_text) < 200:
        warnings.append("page has almost no readable text (it may need JavaScript, or be blocked)")
    return TextResult(f"[PAGE 1]\n{text}", 1, 0, warnings)


def get_text(file_path: str | Path, file_type: str | None = None, *, engine=None,
             cfg: dict | None = None) -> TextResult:
    """
    Read a saved file and return its text (see the module notes above).
    `engine` lets tests (or a cloud OCR service) replace Tesseract.
    """
    cfg = cfg or load_reader_config()
    path = Path(file_path)
    ftype = (file_type or path.suffix.lstrip(".")).lower()
    if ftype in ("htm",):
        ftype = "html"
    if ftype not in SUPPORTED_TYPES:
        raise UnsupportedFileError(f"don't know how to read '{ftype}' files")
    if not path.exists():
        raise FileNotFoundError(str(path))

    ocr_cfg = cfg.get("ocr") or {}
    if ftype == "pdf":
        return _pdf_text(path, engine or get_engine(cfg), ocr_cfg)
    if ftype in IMAGE_TYPES:
        return _image_text(path, engine or get_engine(cfg))
    if ftype == "html":
        return _html_text(path.read_bytes())
    text = _tidy(path.read_bytes().decode("utf-8", errors="replace"))
    return TextResult(f"[PAGE 1]\n{text}", 1, 0)
