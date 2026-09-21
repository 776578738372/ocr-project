"""STATUS: REAL. Shared extractor interface -- [2A] LAYOUT_OCR and
[2B] VLM_EXTRACTION both implement this so the pipeline can call either
without caring which one ran.
"""

from __future__ import annotations

from typing import Protocol

from schemas.schema import ExtractedFields, ExtractionPath


class Extractor(Protocol):
    source: ExtractionPath

    def extract(self, file_bytes: bytes) -> ExtractedFields:
        """Parse a document's raw bytes into structured, confidence-scored fields."""
        ...


def missing_value(source: ExtractionPath, evidence: str | None = None):
    from schemas.schema import ExtractedValue

    return ExtractedValue(value=None, confidence=0.0, source=source, evidence=evidence)
