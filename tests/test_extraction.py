import os

import pytest

from schemas.schema import ExtractionPath
from extraction.classifier import InvalidFileError, classify
from extraction.layout_ocr import LayoutOCRExtractor

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


def test_acroform_extraction_on_real_official_fillable_pdf(samples_dir):
    # Regression test for a real gap: get_text() alone returns nothing for a
    # genuinely fillable PDF, since filled values live in AcroForm widgets,
    # not the text stream. extraction/acroform.py reads those directly.
    with open(os.path.join(samples_dir, "fw9_filled_test_supplier.pdf"), "rb") as f:
        fields = LayoutOCRExtractor().extract(f.read())

    assert fields.legal_name.value == "Apex Industrial Supplies LLC"
    assert fields.dba_name.value == "Apex Industrial Supply"
    assert fields.tax_classification.value == "llc"
    assert fields.tax_classification.llc_subclass == "C"
    assert fields.address.city.value == "Dallas"
    assert fields.address.state.value == "TX"
    assert fields.address.zip.value == "75212"
    assert fields.tin.type == "EIN"
    assert fields.tin.value_masked == "xx-xxx6789"
    # The official template exposes no signature/date widgets at all; this
    # copy has no ink/digital signature applied either, so "not signed" is
    # the correct read, not a symptom of failed extraction.
    assert fields.certification.signed is False
