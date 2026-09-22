"""STATUS: REAL. AcroForm (fillable-PDF) field extraction -- a second,
higher-priority source inside [2A] LAYOUT_OCR.

The official IRS fillable W-9 (irs.gov/pub/irs-pdf/fw9.pdf) is a real
Adobe-authored AcroForm: values a supplier types into it live in form field
*widgets*, not in the page's text stream. `page.get_text()` -- what
layout_ocr.py's regex path reads -- only ever sees the static labels and
instructions, never the filled-in values, on a document like this. This
was a real gap discovered by testing against the actual official PDF, not
a synthetic sample: every field came back null even though the form was
clearly filled out (see docs/design_doc.md §3 / README).

Field-name mapping below is tuned to the official IRS 2024-revision
fillable PDF's own Adobe LiveCycle field names (e.g. "...f1_01[0]" for
Line 1) -- stable because IRS publishes one canonical fillable PDF and the
internal field names don't change between copies of it, only between form
revisions. A differently-authored fillable PDF (e.g. a vendor's own
re-creation of the form) would have different field names and would fall
through to the text-regex path in layout_ocr.py instead -- this is a
targeted fix for the single most common real-world case (someone filling
out and submitting IRS's own published fillable PDF), not a general
solution for arbitrary fillable-PDF authoring tools.
"""

from __future__ import annotations

import re

from schemas.schema import (
    Address,
    Certification,
    ExtractedFields,
    ExtractedValue,
    ExtractionPath,
    TaxClassification,
    TinInfo,
)
from utils.security import mask_tin, tokenize_tin

SOURCE = ExtractionPath.LAYOUT_OCR
_HIGH = 0.97
_MISSING = 0.0

_F = {
    "legal_name": "topmostSubform[0].Page1[0].f1_01[0]",
    "dba_name": "topmostSubform[0].Page1[0].f1_02[0]",
    "llc_subclass": "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].f1_03[0]",
    "other_description": "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].f1_04[0]",
    "exempt_payee_code": "topmostSubform[0].Page1[0].f1_05[0]",
    "fatca_code": "topmostSubform[0].Page1[0].f1_06[0]",
    "address_street": "topmostSubform[0].Page1[0].Address_ReadOrder[0].f1_07[0]",
    "address_city_state_zip": "topmostSubform[0].Page1[0].Address_ReadOrder[0].f1_08[0]",
    "account_numbers": "topmostSubform[0].Page1[0].f1_10[0]",
    "ssn_1": "topmostSubform[0].Page1[0].f1_11[0]",
    "ssn_2": "topmostSubform[0].Page1[0].f1_12[0]",
    "ssn_3": "topmostSubform[0].Page1[0].f1_13[0]",
    "ein_1": "topmostSubform[0].Page1[0].f1_14[0]",
    "ein_2": "topmostSubform[0].Page1[0].f1_15[0]",
}

# Order matches the seven boxes' visual/reading order on the form.
_CHECKBOX_FIELDS = [
    ("individual_sole_proprietor", "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_1[0]"),
    ("c_corporation", "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_1[1]"),
    ("s_corporation", "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_1[2]"),
    ("partnership", "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_1[3]"),
    ("trust_estate", "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_1[4]"),
    ("llc", "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_1[5]"),
    ("other", "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_1[6]"),
]
_FOREIGN_PARTNER_CHECKBOX = "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_2[0]"

_CITY_STATE_ZIP_RE = re.compile(r"(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)")


def _is_checked(value: str | None) -> bool:
    return bool(value) and value != "Off"


def looks_like_this_template(field_values: dict[str, str]) -> bool:
    """True if the PDF's widgets match the official IRS fillable template's
    known field names AND at least one of them is actually populated --
    matching field *names* alone isn't enough. A real gap found via testing:
    a "flattened" copy of this same template can have these exact field
    names present with every value empty (the real values got baked into
    the text stream instead, at extraction/flattened_form.py's expense) --
    without checking for a populated value here, that document would
    wrongly commit to reading empty widgets and return everything null,
    instead of falling through to a path that can actually find the data."""
    if _F["legal_name"] not in field_values and _F["ein_1"] not in field_values:
        return False
    return bool(
        field_values.get(_F["legal_name"], "").strip()
        or field_values.get(_F["ein_1"], "").strip()
        or field_values.get(_F["ssn_1"], "").strip()
    )


def extract_from_widgets(field_values: dict[str, str]) -> ExtractedFields:
    def val(key: str) -> str | None:
        v = field_values.get(_F[key], "").strip()
        return v or None

    legal_name = val("legal_name")
    dba_name = val("dba_name")

    checked = [name for name, field in _CHECKBOX_FIELDS if _is_checked(field_values.get(field))]
    tc_value = checked[0] if len(checked) == 1 else (checked[0] if checked else None)
    tc_confidence = _HIGH if len(checked) == 1 else (_MISSING if not checked else 0.75)
    tc_evidence = (
        f"AcroForm checkbox checked: {checked[0]}" if len(checked) == 1
        else (f"multiple boxes checked: {checked}" if checked else "no checkbox checked")
    )
    llc_subclass = val("llc_subclass") if tc_value == "llc" else None
    foreign_partner_indicator = _is_checked(field_values.get(_FOREIGN_PARTNER_CHECKBOX))

    street = val("address_street")
    city = state = zip_code = None
    csz_raw = val("address_city_state_zip")
    if csz_raw:
        m = _CITY_STATE_ZIP_RE.match(csz_raw)
        if m:
            city, state, zip_code = m.group("city").strip(), m.group("state"), m.group("zip")

    ssn_parts = [val("ssn_1"), val("ssn_2"), val("ssn_3")]
    if val("ein_1") and val("ein_2"):
        tin_type, tin_raw = "EIN", f"{val('ein_1')}-{val('ein_2')}"
    elif all(ssn_parts):
        tin_type, tin_raw = "SSN", f"{ssn_parts[0]}-{ssn_parts[1]}-{ssn_parts[2]}"
    else:
        tin_type, tin_raw = None, None

    def ev(value):
        return _HIGH if value else _MISSING

    return ExtractedFields(
        legal_name=ExtractedValue(value=legal_name, confidence=ev(legal_name), source=SOURCE,
                                   evidence=f"AcroForm field {_F['legal_name']}" if legal_name else None),
        dba_name=ExtractedValue(value=dba_name, confidence=_HIGH, source=SOURCE,
                                 evidence=f"AcroForm field {_F['dba_name']}"),
        tax_classification=TaxClassification(
            value=tc_value, confidence=tc_confidence, source=SOURCE, evidence=tc_evidence,
            llc_subclass=llc_subclass, foreign_partner_indicator=foreign_partner_indicator,
        ),
        address=Address(
            street=ExtractedValue(value=street, confidence=ev(street), source=SOURCE,
                                   evidence=f"AcroForm field {_F['address_street']}" if street else None),
            city=ExtractedValue(value=city, confidence=ev(city), source=SOURCE, evidence=csz_raw),
            state=ExtractedValue(value=state, confidence=ev(state), source=SOURCE, evidence=csz_raw),
            zip=ExtractedValue(value=zip_code, confidence=ev(zip_code), source=SOURCE, evidence=csz_raw),
        ),
        tin=(
            TinInfo(type=tin_type, value_masked=mask_tin(tin_raw), value_token=tokenize_tin(tin_raw),
                    format_valid=True, confidence=_HIGH, source=SOURCE)
            if tin_type else
            TinInfo(type=None, value_masked=None, value_token=None, format_valid=False,
                    confidence=_MISSING, source=SOURCE)
        ),
        # The official template doesn't expose Part II (signature/date) as
        # fillable widgets at all -- IRS requires an ink or digital
        # signature applied outside form-fill. Correctly defaults to
        # "not signed" absent separate ink/signature-annotation detection,
        # which is out of scope here (see design doc's deferred edge cases).
        certification=Certification(signed=False, signature_present=False, date=None, confidence=_MISSING),
    )
