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
    with open(os.path.join(samples_dir, "w9_supplier_1_typed.pdf"), "rb") as f:
        result = classify(f.read())
    assert result.extraction_path == ExtractionPath.LAYOUT_OCR
    assert result.routing_signal == "pdf_embedded_text"


def test_flattened_multipage_pdf_extracts_correctly(samples_dir):
    # Regression test for a real gap found via testing against real documents:
    # a "flattened" fillable PDF has all AcroForm widgets empty (the values
    # got baked into the text stream instead, after the page footer -- see
    # extraction/flattened_form.py), AND this specific document is
    # multi-page, so the footer marker repeats once per page. Naively taking
    # the LAST marker occurrence (rather than the first) lands on the final
    # page's footer and finds nothing -- this asserts the real, fixed
    # behavior end to end.
    with open(os.path.join(samples_dir, "w9_supplier_1_typed.pdf"), "rb") as f:
        fields = LayoutOCRExtractor().extract(f.read())

    assert fields.legal_name.value == "Apex Industrial Supplies LLC"
    assert fields.dba_name.value == "Apex Industrial Supply"
    assert fields.address.street.value == "2450 West Commerce Street, Suite 310"
    assert fields.address.city.value == "Dallas"
    assert fields.address.state.value == "TX"
    assert fields.address.zip.value == "75212"
    assert fields.tin.type == "EIN"
    assert fields.tin.value_masked == "xx-xxx6789"
    assert fields.certification.signed is True
    assert fields.certification.date == "09/22/2026"


def test_acroform_widget_detection_requires_a_populated_value(samples_dir):
    # Regression test for a related bug in the same investigation:
    # looks_like_this_template() originally matched on field NAMES alone.
    # This document's widget field names happen to match the official
    # template's naming convention exactly, but every widget is empty (the
    # real values are flattened text, per the test above) -- without
    # checking for a populated value, extract() would wrongly commit to the
    # (empty) AcroForm path and return every field as null.
    from extraction.acroform import looks_like_this_template

    empty_widgets = {"topmostSubform[0].Page1[0].f1_01[0]": "", "topmostSubform[0].Page1[0].f1_14[0]": ""}
    assert looks_like_this_template(empty_widgets) is False

    populated_widgets = {"topmostSubform[0].Page1[0].f1_01[0]": "Acme Inc"}
    assert looks_like_this_template(populated_widgets) is True
