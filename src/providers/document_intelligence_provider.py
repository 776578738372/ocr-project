"""STATUS: REAL (activates when AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT and
AZURE_DOCUMENT_INTELLIGENCE_KEY are both set). Unified extraction engine for
*both* FSM extraction states -- [2A] OCR_EXTRACTION and [2B] VLM_EXTRACTION
-- via Azure AI Document Intelligence's prebuilt-layout model.
decision/pipeline.py checks for these two env vars before choosing an
extractor for either branch; when set, this class replaces both
extraction/layout_ocr.py (for PDFs) and the vision-LLM providers this
project used before for scans/photos (retired -- see docs/design_doc.md
§3.3) with the one engine, since prebuilt-layout takes a PDF or an image as
the same kind of input and returns the same kind of output either way --
there's no format-specific code path to maintain.

Why this exists: real-document testing surfaced a concrete accuracy gap in
that earlier vision-LLM path -- GPT-4o read a checked "Partnership" box as
"C Corporation" with self-reported confidence 1.0 (see docs/design_doc.md
§3.3). A general vision-language model reasons about the whole image at once
and has no dedicated mechanism for checkbox state; Document Intelligence's
prebuilt-layout model does -- selection marks are a first-class,
purpose-built model output (a bounding polygon plus a selected/unselected
state), not a language model's guess. Tax classification is resolved here by
finding, for each mark reported "selected", the nearest OCR'd text line
among the seven known checkbox labels -- the same position-matching idea
already used for flattened PDFs in extraction/flattened_form.py, just
against Document Intelligence's polygons instead of PyMuPDF widget rects.

The remaining fields (name, address, TIN, signature) are read from
result.content, Document Intelligence's own OCR'd, reading-order text --
using the same label-anchored regex approach extraction/layout_ocr.py uses
on a PDF's own embedded text layer, since a real OCR transcription of a W-9
reads like the same text either source would contain.

`source` on each instance records which FSM state it's serving (see the
class docstring below) -- it is not which vendor ran the extraction, since
that's now the same vendor for both states.
"""

from __future__ import annotations

import os
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

SOURCE = ExtractionPath.VLM_EXTRACTION

# Anchored on the actual 2024-revision W-9 label text, as Document
# Intelligence's OCR reports it (verified against live output -- see
# docs/design_doc.md §3.3). This wording is notably different from the
# pre-2024 form extraction/layout_ocr.py was originally anchored on ("Name
# (as shown on your income tax return)"), and these labels don't end in a
# colon the way that older wording did, so `:?` (not a required `:`) is
# deliberate throughout, not a leftover.
_LEGAL_NAME_RE = re.compile(
    r"1\s+Name of entity/individual\.[^\n]*?\)\s*(?P<value>[^\n]*)",
    re.IGNORECASE,
)
_DBA_RE = re.compile(
    r"2\s+Business name/disregarded entity name, if different from above\.\s*(?P<value>[^\n]*)",
    re.IGNORECASE,
)
_STREET_RE = re.compile(
    r"5\s+Address\s*\(number, street.*?See instructions\.\s*(?P<value>[^\n]*)", re.IGNORECASE
)
_CITY_STATE_ZIP_RE = re.compile(
    r"6\s+City, state, and ZIP code:?\s*(?P<city>[^,\n]*),\s*"
    r"(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)",
    re.IGNORECASE,
)
# Separators are `[-\s]`, not a literal hyphen, because OCR on a real
# photographed/handwritten form (unlike a clean typed PDF) sometimes reads a
# handwritten hyphen as a gap -- found live: "000-12 3456" for an SSN whose
# second separator should be "-". utils/security.py strips non-digits before
# hashing/masking, so the separator character itself is never load-bearing.
_EIN_RE = re.compile(r"\b(\d{2})[-\s](\d{7})\b")
_SSN_RE = re.compile(r"\b(\d{3})[-\s](\d{2})[-\s](\d{4})\b")
_LLC_SUBCLASS_RE = re.compile(
    r"C\s*=\s*C corporation.*?P\s*=\s*Partnership\)[^\n]{0,30}?\b([CSP])\b",
    re.IGNORECASE | re.DOTALL,
)
_SIGNATURE_LABEL_RE = re.compile(r"Signature of\s+U\.S\.\s*person", re.IGNORECASE)
_DATE_LABEL_RE = re.compile(r"Date\s*[▸►]?[ \t]*(?P<date>\d{1,2}/\d{1,2}/\d{2,4})")

_CHECKBOX_LABELS = [
    ("individual_sole_proprietor", ("individual/sole proprietor",)),
    ("c_corporation", ("c corporation",)),
    ("s_corporation", ("s corporation",)),
    ("partnership", ("partnership",)),
    ("trust_estate", ("trust/estate", "trust estate")),
    ("llc", ("llc",)),
    ("other", ("other",)),
]

_HIGH = 0.92
_MEDIUM = 0.7
_MISSING = 0.0


def _polygon_topleft(polygon: list[float]) -> tuple[float, float]:
    """The checkbox for a label always sits immediately left of that label's
    own top-left corner, at roughly the same y -- unlike the corner, a
    line's *centroid* shifts right by however long its text happens to be,
    which skews distance comparisons between labels of very different
    lengths (e.g. "Other" vs the much longer "LLC. Enter the tax
    classification (C = C corporation, ...)" line). Verified against live
    output: centroid-based matching mis-resolved a real document's checked
    LLC box as "individual_sole_proprietor" (individual's short label has a
    centroid deceptively close to the mark); the corner fixes it."""
    return polygon[0], polygon[1]


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _label_key_for_line(text: str) -> str | None:
    """Matches on the label starting the line, not substring containment --
    the LLC line's own text ("LLC. Enter the tax classification (C = C
    corporation, S = S corporation, P = Partnership)") mentions every other
    classification by name as part of its instructions, so a bare `in`
    check wrongly tagged that whole line as "c_corporation" (found via live
    testing: it sits closer to a checked LLC mark than the real, short "C
    corporation" line does, so the mismatch was picked as the "nearest"
    match)."""
    lowered = text.lower().lstrip()
    for key, needles in _CHECKBOX_LABELS:
        if any(lowered.startswith(needle) for needle in needles):
            return key
    return None


def _resolve_tax_classification(page) -> tuple[str | None, str]:
    """For each mark Document Intelligence reports as "selected", find the
    nearest checkbox-label line by polygon distance -- mirrors
    extraction/flattened_form.py's widget-rect position matching, but keyed
    off OCR'd label text instead of a known widget field name."""
    selected = [m for m in (page.selection_marks or []) if m.state == "selected"]
    if not selected:
        return None, "no selection mark reported as selected"

    label_lines = [
        (key, _polygon_topleft(line.polygon))
        for line in (page.lines or [])
        for key in [_label_key_for_line(line.content)]
        if key is not None and line.polygon
    ]
    if not label_lines:
        return None, "no checkbox-label text recognized near any selected mark"

    matches = []
    for mark in selected:
        if not mark.polygon:
            continue
        mark_anchor = _polygon_topleft(mark.polygon)
        key, _ = min(label_lines, key=lambda kc: _distance(mark_anchor, kc[1]))
        matches.append(key)

    if not matches:
        return None, "selected mark(s) had no usable polygon"
    if len(matches) == 1:
        return matches[0], f"selection mark position-matched to label: {matches[0]}"
    # More than one box reported selected is a validation problem, not an
    # extraction failure -- report the first and let validation/rules.py
    # raise the real flag.
    return matches[0], f"multiple marks reported selected: {matches}"


class AzureDocumentIntelligenceExtractor:
    """One engine, used for both FSM extraction states ([2A] OCR_EXTRACTION
    and [2B] VLM_EXTRACTION) -- prebuilt-layout handles a clean digital PDF
    and a phone photo the same way. `source` is which FSM state this
    instance is serving, not which vendor ran -- validation/rules.py's
    "possible wrong form" heuristic is specifically about a *genuine
    embedded-text-layer PDF* finding no field anchors, which only holds for
    the [2A] case, so pipeline.py instantiates this twice with different
    `source` values rather than once with a fixed one."""

    def __init__(self, source: ExtractionPath = SOURCE):
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential

        endpoint = os.environ["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"]
        key = os.environ["AZURE_DOCUMENT_INTELLIGENCE_KEY"]
        self._client = DocumentIntelligenceClient(endpoint, AzureKeyCredential(key))
        self.source = source

    def extract(self, file_bytes: bytes) -> ExtractedFields:
        poller = self._client.begin_analyze_document("prebuilt-layout", body=file_bytes)
        result = poller.result()
        text = result.content or ""
        page = result.pages[0] if result.pages else None

        llc_subclass = None
        if page is not None:
            tax_value, tc_evidence = _resolve_tax_classification(page)
        else:
            tax_value, tc_evidence = None, "document intelligence returned no pages"
        if tax_value == "llc":
            sub = _LLC_SUBCLASS_RE.search(text)
            llc_subclass = sub.group(1).upper() if sub else None

        return ExtractedFields(
            legal_name=self._match(text, _LEGAL_NAME_RE),
            dba_name=self._match(text, _DBA_RE, allow_blank=True),
            tax_classification=TaxClassification(
                value=tax_value,
                confidence=_HIGH if tax_value else _MISSING,
                source=self.source,
                evidence=tc_evidence,
                llc_subclass=llc_subclass,
            ),
            address=self._address(text),
            tin=self._tin(text),
            certification=self._certification(text),
        )

    def _match(self, text: str, pattern: re.Pattern, allow_blank: bool = False) -> ExtractedValue:
        m = pattern.search(text)
        value = (m.group("value").strip() if m else None) or None
        matched = bool(m) if allow_blank else bool(m and value)
        return ExtractedValue(
            value=value, confidence=_HIGH if matched else _MISSING, source=self.source,
            evidence=m.group(0) if m else None,
        )

    def _address(self, text: str) -> Address:
        street = self._match(text, _STREET_RE)
        csz = _CITY_STATE_ZIP_RE.search(text)
        if csz:
            city = ExtractedValue(value=csz.group("city").strip(), confidence=_HIGH, source=self.source, evidence=csz.group(0))
            state = ExtractedValue(value=csz.group("state").strip().upper(), confidence=_HIGH, source=self.source, evidence=csz.group(0))
            zip_code = ExtractedValue(value=csz.group("zip").strip(), confidence=_HIGH, source=self.source, evidence=csz.group(0))
        else:
            city = ExtractedValue(value=None, confidence=_MISSING, source=self.source)
            state = ExtractedValue(value=None, confidence=_MISSING, source=self.source)
            zip_code = ExtractedValue(value=None, confidence=_MISSING, source=self.source)
        return Address(street=street, city=city, state=state, zip=zip_code)

    def _tin(self, text: str) -> TinInfo:
        part1_idx = text.lower().find("part i")
        scope = text[part1_idx:] if part1_idx != -1 else text
        ein_m = _EIN_RE.search(scope)
        ssn_m = _SSN_RE.search(scope)
        if ein_m:
            raw, tin_type = "".join(ein_m.groups()), "EIN"
        elif ssn_m:
            raw, tin_type = "".join(ssn_m.groups()), "SSN"
        else:
            return TinInfo(type=None, value_masked=None, value_token=None, format_valid=False, confidence=_MISSING, source=self.source)
        return TinInfo(
            type=tin_type, value_masked=mask_tin(raw), value_token=tokenize_tin(raw),
            format_valid=True, confidence=_HIGH, source=self.source,
        )

    def _certification(self, text: str) -> Certification:
        sig_m = _SIGNATURE_LABEL_RE.search(text)
        date_m = _DATE_LABEL_RE.search(text, sig_m.end()) if sig_m else None
        date = date_m.group("date") if date_m else None
        # A handwritten signature isn't machine text, so OCR can't transcribe
        # it -- but a real signature still produces *some* (likely garbled)
        # word tokens between the two labels, where a truly blank line
        # produces none. That gap is what this checks, mirroring
        # extraction/layout_ocr.py's date-presence proxy for the PDF path.
        between = text[sig_m.end():date_m.start()] if (sig_m and date_m) else ""
        signature_present = bool(between.strip())
        return Certification(
            signed=signature_present and date is not None,
            signature_present=signature_present,
            date=date,
            confidence=_HIGH if signature_present else _MISSING,
        )
