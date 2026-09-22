"""STATUS: REAL. [2A] LAYOUT_OCR: deterministic extraction for digital PDFs
with an embedded text layer.

Two extraction sources, tried in order:
  1. AcroForm widget fields (extraction/acroform.py) -- checked first, since
     a genuinely fillable PDF (like IRS's own published fillable W-9) stores
     typed values in form fields, not in the page's text stream at all.
     Discovered as a real gap by testing against the actual official PDF:
     every field came back null despite the form being clearly filled out,
     because get_text() only ever sees the static labels. See acroform.py's
     docstring for the field-name mapping and its scope (tuned to IRS's own
     template specifically).
  2. Text-stream regexes, as before -- PyMuPDF pulls the actual text stream
     out of the PDF, and a set of regexes anchored on the W-9's own field
     labels ("1 Name (as shown on your income tax return):", "Part I
     Taxpayer Identification Number (TIN):", etc.) locates each field. This
     is what runs for a flattened/printed-and-typed/non-fillable PDF, where
     there's no widget layer to read at all.

Because the text is a native digital layer rather than a raster scan,
there's no OCR-token confidence to report -- confidence here reflects
structural certainty: did the label anchor (or AcroForm field) match, and
does the captured value pass its own format check (TIN shape, state code,
zip, checkbox exclusivity).

Known scope cut: checkbox *state* (checked vs. unchecked) is read as literal
"[X]" / "[ ]" text tokens. A real scanned or hand-marked form doesn't render
checkboxes as text at all -- that's precisely why this path only ever runs on
documents the classifier has already confirmed have a genuine embedded text
layer. Anything without one (scans, photos, wrong-form-entirely) is routed to
[2B] VLM_EXTRACTION instead, where a vision model reads the mark directly.
"""

from __future__ import annotations

import re

import pymupdf as fitz

from extraction.acroform import extract_from_widgets, looks_like_this_template
from extraction.flattened_form import extract_from_flattened_tail, looks_like_flattened_tail
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

_LEGAL_NAME_RE = re.compile(
    r"1\s+Name\s*\(as shown on your income tax return\):[ \t]*(?P<value>[^\n]*)",
    re.IGNORECASE,
)
_DBA_RE = re.compile(
    r"2\s+Business name.*?:[ \t]*(?P<value>[^\n]*)", re.IGNORECASE
)
_CHECKBOX_RE = re.compile(
    r"\[(X| )\]\s*(Individual/sole proprietor|Limited liability company|"
    r"C Corporation|S Corporation|Partnership|Trust/estate|Other)",
    re.IGNORECASE,
)
_LLC_SUBCLASS_RE = re.compile(r"C,\s*S,\s*or\s*P\):[ \t]*([CSP])", re.IGNORECASE)
_LINE_3B_RE = re.compile(
    r"3b\s+Foreign partner.*?\[(X| )\]", re.IGNORECASE | re.DOTALL
)
_STREET_RE = re.compile(
    r"5\s+Address\s*\(number, street.*?\):[ \t]*(?P<value>[^\n]*)", re.IGNORECASE
)
_CITY_STATE_ZIP_RE = re.compile(
    r"6\s+City, state, and ZIP code:\s*(?P<city>[^,\n]*),\s*"
    r"(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)",
    re.IGNORECASE,
)
_EIN_RE = re.compile(r"\b(\d{2}-\d{7})\b")
_SSN_RE = re.compile(r"\b(\d{3}-\d{2}-\d{4})\b")
_SIGNATURE_RE = re.compile(
    r"Signature of U\.S\. person:[ \t]*(?P<value>[^\n]*?)[ \t]+Date:[ \t]*(?P<date>[^\n]*)",
    re.IGNORECASE,
)

_LABEL_TO_VALUE = {
    "individual/sole proprietor": "individual_sole_proprietor",
    "limited liability company": "llc",
    "c corporation": "c_corporation",
    "s corporation": "s_corporation",
    "partnership": "partnership",
    "trust/estate": "trust_estate",
    "other": "other",
}

_HIGH = 0.97
_MEDIUM = 0.75
_MISSING = 0.0


def _extracted(value: str | None, matched: bool, evidence: str | None) -> ExtractedValue:
    confidence = _HIGH if (matched and value) else _MISSING
    return ExtractedValue(value=value, confidence=confidence, source=SOURCE, evidence=evidence)


class LayoutOCRExtractor:
    source = SOURCE

    def extract(self, file_bytes: bytes) -> ExtractedFields:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            field_values = {
                w.field_name: w.field_value
                for page in doc for w in page.widgets()
            }
            if looks_like_this_template(field_values):
                return extract_from_widgets(field_values)

            text = "\n".join(page.get_text() for page in doc)

            fields = ExtractedFields(
                legal_name=self._legal_name(text),
                dba_name=self._dba_name(text),
                tax_classification=self._tax_classification(text),
                address=self._address(text),
                tin=self._tin(text),
                certification=self._certification(text),
            )

            # Third pattern (extraction/flattened_form.py): label-anchored
            # regex found nothing, but the text has the shape of a flattened
            # AcroForm (values appended after the page footer, disconnected
            # from labels). Pass page 1 (still open here) so tax
            # classification can be resolved via checkbox-widget position.
            if not fields.legal_name.value and looks_like_flattened_tail(text):
                flattened = extract_from_flattened_tail(text, page=doc[0])
                if flattened is not None:
                    return flattened

            return fields
        finally:
            doc.close()

    def _legal_name(self, text: str) -> ExtractedValue:
        m = _LEGAL_NAME_RE.search(text)
        value = m.group("value").strip() if m else None
        return _extracted(value, bool(m and value), m.group(0) if m else None)

    def _dba_name(self, text: str) -> ExtractedValue:
        m = _DBA_RE.search(text)
        value = m.group("value").strip() if m else None
        value = value or None
        # A blank DBA is a legitimate, confidently-known "no value" -- not a
        # missing field -- so it still gets high confidence.
        confidence = _HIGH if m else _MISSING
        return ExtractedValue(
            value=value, confidence=confidence, source=SOURCE,
            evidence=m.group(0) if m else None,
        )

    def _tax_classification(self, text: str) -> TaxClassification:
        checks = _CHECKBOX_RE.findall(text)
        checked = [label for mark, label in checks if mark.upper() == "X"]

        if len(checked) == 1:
            label = checked[0].lower()
            value = _LABEL_TO_VALUE.get(label)
            confidence = _HIGH
            evidence = f"[X] {checked[0]}"
        elif len(checked) == 0:
            value, confidence, evidence = None, _MISSING, "no checkbox marked"
        else:
            # More than one box checked is a validation problem, not an
            # extraction failure -- report the first with reduced confidence
            # and let validation/rules.py raise the real flag.
            label = checked[0].lower()
            value = _LABEL_TO_VALUE.get(label)
            confidence = _MEDIUM
            evidence = f"multiple boxes checked: {checked}"

        llc_subclass = None
        if value == "llc":
            sub = _LLC_SUBCLASS_RE.search(text)
            llc_subclass = sub.group(1).upper() if sub else None

        line_3b = _LINE_3B_RE.search(text)
        foreign_partner_indicator = (
            line_3b.group(1).upper() == "X" if line_3b else None
        )

        return TaxClassification(
            value=value,
            confidence=confidence,
            source=SOURCE,
            evidence=evidence,
            llc_subclass=llc_subclass,
            foreign_partner_indicator=foreign_partner_indicator,
        )

    def _address(self, text: str) -> Address:
        street_m = _STREET_RE.search(text)
        street_val = street_m.group("value").strip() if street_m else None
        street = _extracted(street_val, bool(street_m and street_val), street_m.group(0) if street_m else None)

        csz_m = _CITY_STATE_ZIP_RE.search(text)
        if csz_m:
            city = _extracted(csz_m.group("city").strip(), True, csz_m.group(0))
            state = _extracted(csz_m.group("state").strip().upper(), True, csz_m.group(0))
            zip_code = _extracted(csz_m.group("zip").strip(), True, csz_m.group(0))
        else:
            city = _extracted(None, False, None)
            state = _extracted(None, False, None)
            zip_code = _extracted(None, False, None)

        return Address(street=street, city=city, state=state, zip=zip_code)

    def _tin(self, text: str) -> TinInfo:
        part1_idx = text.lower().find("part i")
        scope = text[part1_idx:] if part1_idx != -1 else text

        ein_m = _EIN_RE.search(scope)
        ssn_m = _SSN_RE.search(scope)

        if ein_m:
            raw, tin_type = ein_m.group(1), "EIN"
        elif ssn_m:
            raw, tin_type = ssn_m.group(1), "SSN"
        else:
            return TinInfo(
                type=None, value_masked=None, value_token=None,
                format_valid=False, confidence=_MISSING, source=SOURCE,
            )

        return TinInfo(
            type=tin_type,
            value_masked=mask_tin(raw),
            value_token=tokenize_tin(raw),
            format_valid=True,
            confidence=_HIGH,
            source=SOURCE,
        )

    def _certification(self, text: str) -> Certification:
        m = _SIGNATURE_RE.search(text)
        if not m:
            return Certification(
                signed=False, signature_present=False, date=None, confidence=_MISSING
            )
        signer = m.group("value").strip()
        date = m.group("date").strip() or None
        signature_present = bool(signer)
        return Certification(
            signed=signature_present and date is not None,
            signature_present=signature_present,
            date=date,
            confidence=_HIGH if signature_present else _MEDIUM,
        )
