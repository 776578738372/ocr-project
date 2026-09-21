"""STATUS: REAL. [1] INGESTED -> routing decision for [2A] LAYOUT_OCR vs
[2B] VLM_EXTRACTION.

Implements the 3-stage probe:
  1. File format / magic bytes -> PNG/JPEG always goes to VLM (handles lens
     distortion, noise, phone photos natively); anything else unsupported is
     rejected before any extraction work happens.
  2. PDF vector-text probe (PyMuPDF) -> a PDF with a real embedded text layer
     is a candidate for the deterministic layout-OCR path; a PDF that is just
     a scanned image wrapper has ~0 embedded characters and goes to VLM.
  3. Text-density fallback -> a PDF that clears the character-count bar but
     has implausibly little text per page (e.g. a mostly-scanned page with a
     stray text watermark) is downgraded to VLM. This substitutes for a real
     DPI/OCR-confidence probe, which only makes sense once there's an actual
     raster OCR engine (e.g. Azure Document Intelligence) in the loop -- see
     the design doc for why the prototype approximates it this way.
"""

from __future__ import annotations

import pymupdf as fitz
from dataclasses import dataclass

from schemas.schema import ExtractionPath

SUPPORTED_IMAGE_MIME = {"image/png", "image/jpeg"}
PDF_MAGIC = b"%PDF"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

MIN_EMBEDDED_CHARS = 100
MIN_CHARS_PER_PAGE = 300  # a real W-9 page runs ~800-1500 extractable chars


class InvalidFileError(Exception):
    """Raised for [1] INGESTED --(INVALID_FILE_TYPE)--> [6D] REJECTED_INVALID."""


@dataclass(frozen=True)
class ClassificationResult:
    extraction_path: ExtractionPath
    routing_signal: str


def _sniff_format(file_bytes: bytes) -> str:
    if file_bytes.startswith(PDF_MAGIC):
        return "pdf"
    if file_bytes.startswith(PNG_MAGIC):
        return "png"
    if file_bytes.startswith(JPEG_MAGIC):
        return "jpeg"
    return "unknown"


def classify(file_bytes: bytes) -> ClassificationResult:
    fmt = _sniff_format(file_bytes)

    if fmt in ("png", "jpeg"):
        return ClassificationResult(ExtractionPath.VLM_EXTRACTION, "image_format")

    if fmt != "pdf":
        raise InvalidFileError(
            "Input payload must be a valid PDF, PNG, or JPEG under 20MB."
        )

    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as exc:  # pragma: no cover - fitz raises various types
        raise InvalidFileError(f"Corrupted or unreadable PDF: {exc}") from exc

    try:
        page_count = doc.page_count
        if page_count == 0:
            raise InvalidFileError("PDF contains no pages.")

        text = "".join(page.get_text() for page in doc)
        char_count = len(text.strip())

        if char_count < MIN_EMBEDDED_CHARS:
            return ClassificationResult(
                ExtractionPath.VLM_EXTRACTION, "pdf_no_embedded_text"
            )

        density = char_count / page_count
        if density < MIN_CHARS_PER_PAGE:
            return ClassificationResult(
                ExtractionPath.VLM_EXTRACTION, "pdf_low_text_density"
            )

        return ClassificationResult(ExtractionPath.LAYOUT_OCR, "pdf_embedded_text")
    finally:
        doc.close()
