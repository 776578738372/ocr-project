from schemas.schema import (
    Address,
    Certification,
    ExtractedFields,
    ExtractedValue,
    ExtractionPath,
    TaxClassification,
    TinInfo,
)
from validation.rules import has_blocking_flag, validate

SOURCE = ExtractionPath.LAYOUT_OCR


def _field(value, confidence=0.97):
    return ExtractedValue(value=value, confidence=confidence, source=SOURCE)


def make_fields(**overrides) -> ExtractedFields:
    defaults = dict(
        legal_name=_field("Acme Corporation"),
        dba_name=_field(None),
        tax_classification=TaxClassification(value="c_corporation", confidence=0.97, source=SOURCE),
        address=Address(street=_field("100 Main St"), city=_field("Columbus"), state=_field("OH"), zip=_field("43215")),
        tin=TinInfo(type="EIN", value_masked="xx-xxx1093", value_token="tin_tok_abc", format_valid=True, confidence=0.97, source=SOURCE),
        certification=Certification(signed=True, signature_present=True, date="09/15/2026", confidence=0.97),
    )
    defaults.update(overrides)
    return ExtractedFields(**defaults)


def test_clean_fields_produce_no_blocking_flags():
    flags = validate(make_fields())
    assert not has_blocking_flag(flags)


def test_mock_compliance_checks_are_always_reported_as_not_performed():
    # MOCK: validation/compliance.py has no real IRS/OFAC integration -- every
    # response should honestly say so via INFO flags, never silently omit them.
    codes = [f.code for f in validate(make_fields())]
    assert "INFO_TIN_MATCHING_NOT_PERFORMED" in codes
    assert "INFO_OFAC_SCREENING_NOT_PERFORMED" in codes
    assert not has_blocking_flag(validate(make_fields()))  # INFO, never blocking


def test_missing_legal_name_is_blocking():
    flags = validate(make_fields(legal_name=_field(None, confidence=0.0)))
    codes = [f.code for f in flags]
    assert "ERR_MISSING_LEGAL_NAME" in codes
    assert has_blocking_flag(flags)


def test_invalid_tin_is_blocking():
    fields = make_fields(tin=TinInfo(type=None, value_masked=None, value_token=None, format_valid=False, confidence=0.0, source=SOURCE))
    flags = validate(fields)
    codes = [f.code for f in flags]
    assert "ERR_INVALID_TIN" in codes
    assert has_blocking_flag(flags)


def test_unsigned_form_is_blocking():
    fields = make_fields(certification=Certification(signed=False, signature_present=False, date=None, confidence=0.0))
    flags = validate(fields)
    codes = [f.code for f in flags]
    assert "ERR_NOT_SIGNED" in codes
    assert has_blocking_flag(flags)


def test_wrong_form_detected_when_layout_ocr_finds_nothing():
    fields = make_fields(
        legal_name=_field(None, confidence=0.0),
        tin=TinInfo(type=None, value_masked=None, value_token=None, format_valid=False, confidence=0.0, source=SOURCE),
    )
    codes = [f.code for f in validate(fields)]
    assert "ERR_POSSIBLE_WRONG_FORM" in codes


def test_corporation_with_ssn_instead_of_ein_is_flagged():
    # Per the case study's own reference material and the real W-9
    # instructions: a corporation cannot have a personal SSN as its TIN.
    fields = make_fields(
        tax_classification=TaxClassification(value="c_corporation", confidence=0.97, source=SOURCE),
        tin=TinInfo(type="SSN", value_masked="xxx-xx-1234", value_token="tin_tok_xyz", format_valid=True, confidence=0.97, source=SOURCE),
    )
    codes = [f.code for f in validate(fields)]
    assert "WARN_TIN_TYPE_INCONSISTENT_WITH_CLASSIFICATION" in codes


def test_sole_proprietor_with_ssn_is_not_flagged():
    # Individual/sole proprietor is explicitly allowed to use either SSN or EIN.
    fields = make_fields(
        tax_classification=TaxClassification(value="individual_sole_proprietor", confidence=0.97, source=SOURCE),
        tin=TinInfo(type="SSN", value_masked="xxx-xx-1234", value_token="tin_tok_xyz", format_valid=True, confidence=0.97, source=SOURCE),
    )
    codes = [f.code for f in validate(fields)]
    assert "WARN_TIN_TYPE_INCONSISTENT_WITH_CLASSIFICATION" not in codes


def test_corporation_with_ein_is_not_flagged():
    codes = [f.code for f in validate(make_fields())]  # default fixture: c_corporation + EIN
    assert "WARN_TIN_TYPE_INCONSISTENT_WITH_CLASSIFICATION" not in codes


def test_llc_without_subclass_warns():
    fields = make_fields(
        tax_classification=TaxClassification(value="llc", confidence=0.97, source=SOURCE, llc_subclass=None)
    )
    codes = [f.code for f in validate(fields)]
    assert "WARN_LLC_SUBCLASS_MISSING" in codes


def test_invalid_state_code_warns():
    fields = make_fields(address=Address(
        street=_field("100 Main St"), city=_field("Columbus"), state=_field("ZZ"), zip=_field("43215"),
    ))
    codes = [f.code for f in validate(fields)]
    assert "WARN_INVALID_STATE_CODE" in codes
