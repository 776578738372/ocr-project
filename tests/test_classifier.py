import os

import pytest

from w9_onboarding.contracts.schema import ExtractionPath
from w9_onboarding.ingestion.classifier import InvalidFileError, classify

PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG_MAGIC = b"\xff\xd8\xff" + b"\x00" * 32


def test_png_routes_to_vlm():
    result = classify(PNG_MAGIC)
    assert result.extraction_path == ExtractionPath.VLM_EXTRACTION
    assert result.routing_signal == "image_format"


def test_jpeg_routes_to_vlm():
    result = classify(JPEG_MAGIC)
    assert result.extraction_path == ExtractionPath.VLM_EXTRACTION
    assert result.routing_signal == "image_format"


def test_unsupported_bytes_raise_invalid_file_error():
    with pytest.raises(InvalidFileError):
        classify(b"not a real document, just plain bytes")


def test_clean_digital_pdf_routes_to_layout_ocr(samples_dir):
    with open(os.path.join(samples_dir, "clean_w9_acme.pdf"), "rb") as f:
        result = classify(f.read())
    assert result.extraction_path == ExtractionPath.LAYOUT_OCR
    assert result.routing_signal == "pdf_embedded_text"


def test_scanned_pdf_wrapper_with_no_text_layer_routes_to_vlm(samples_dir):
    with open(os.path.join(samples_dir, "scanned_wrapper_w9.pdf"), "rb") as f:
        result = classify(f.read())
    assert result.extraction_path == ExtractionPath.VLM_EXTRACTION
    assert result.routing_signal == "pdf_no_embedded_text"
