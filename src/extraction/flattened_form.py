"""STATUS: REAL. A third real-world PDF pattern, found via testing against
more real filled documents: a "flattened" copy of the fillable W-9 where the
AcroForm widgets are all empty (see extraction/acroform.py), but the typed
values were baked into the page's text content stream anyway -- appended
sequentially after the page footer ("Cat. No. 10231X" / "Form W-9 (Rev.
3-2024)"), in field/tab order, completely disconnected from their visual
labels. Neither the AcroForm path (widgets are empty) nor the label-anchored
regex path (values aren't adjacent to their labels) can read this.

This is a narrower, more heuristic extraction than the other two: it relies
on a few recognizable anchors (TIN format, city/state/zip shape, a date) to
find its footing in an otherwise unlabeled block of lines, then reads the
free-text fields (name, DBA, street, signer) positionally relative to those
anchors.

Tax classification needed a second pass to solve properly: the flattened
text has only a lone "X" with no label context, so which of the 7 checkboxes
it belongs to can't be read from text alone. The fix: the AcroForm widgets
for this document are empty (that's *why* this fallback runs at all), but
their `.rect` positions are still exactly where the real checkboxes are
drawn -- so the lone "X" word's position (from `page.get_text("words")`,
which does carry bounding boxes) can be matched to whichever checkbox
widget's rect is nearest. This turned a real gap ("do we even know it's an
LLC?") into a real answer, and in testing caught a case where a guess from
the entity name alone ("...Inc." implying C-corp) would have been wrong --
the document's actual checked box was S-corp.
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
_HIGH = 0.9  # slightly below the label-anchored/AcroForm paths' 0.97: positional, not label-confirmed
_MISSING = 0.0

_FOOTER_MARKER = "Form W-9 (Rev. 3-2024)"
_CITY_STATE_ZIP_RE = re.compile(r"^(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$")
_EIN_RE = re.compile(r"^\d{2}-\d{7}$")
_SSN_RE = re.compile(r"^\d{3}-\d{2}-\d{4}$")
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")

# Same 7 boxes, same widget-name pattern as acroform.py's _CHECKBOX_FIELDS --
# duplicated rather than imported because this module reads their .rect
# (position), not their .field_value (which is empty here by definition).
_CHECKBOX_FIELDS = [
    ("individual_sole_proprietor", "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_1[0]"),
    ("c_corporation", "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_1[1]"),
    ("s_corporation", "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_1[2]"),
    ("partnership", "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_1[3]"),
    ("trust_estate", "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_1[4]"),
    ("llc", "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_1[5]"),
    ("other", "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_1[6]"),
]
_FOREIGN_PARTNER_FIELD = "topmostSubform[0].Page1[0].Boxes3a-b_ReadOrder[0].c1_2[0]"
_MAX_MARK_DISTANCE_PT = 20.0  # checkbox marks sit within ~10pt of their widget on this template


def _rect_center(rect) -> tuple[float, float]:
    x0, y0, x1, y1 = rect[0], rect[1], rect[2], rect[3]
    return (x0 + x1) / 2, (y0 + y1) / 2


def _nearest_checkbox(x_marks: list, field_order: list[tuple[str, str]], widget_rects: dict) -> tuple[str | None, str | None, float]:
    best_value, best_evidence, best_dist = None, None, float("inf")
    for value, field_name in field_order:
        rect = widget_rects.get(field_name)
        if rect is None:
            continue
        wc = _rect_center(rect)
        for mark in x_marks:
            mc = _rect_center(mark[:4])
            dist = ((wc[0] - mc[0]) ** 2 + (wc[1] - mc[1]) ** 2) ** 0.5
            if dist < best_dist:
                best_dist, best_value = dist, value
                best_evidence = f"'X' mark nearest {field_name} widget ({dist:.1f}pt away)"
    return best_value, best_evidence, best_dist


def _detect_checked_classification(page) -> tuple[str | None, bool | None, str | None]:
    """Cross-references the lone flattened 'X' mark's position against the
    (empty-valued but correctly positioned) checkbox widget rects. Returns
    (classification_value, foreign_partner_indicator, evidence)."""
    widget_rects = {w.field_name: w.rect for w in page.widgets()}
    x_marks = [w for w in page.get_text("words") if w[4] == "X"]
    if not x_marks or not widget_rects:
        return None, None, None

    value, evidence, dist = _nearest_checkbox(x_marks, _CHECKBOX_FIELDS, widget_rects)
    if dist > _MAX_MARK_DISTANCE_PT:
        value, evidence = None, None

    foreign_partner = None
    fp_rect = widget_rects.get(_FOREIGN_PARTNER_FIELD)
    if fp_rect is not None:
        fp_center = _rect_center(fp_rect)
        fp_dist = min(
            (((fp_center[0] - _rect_center(m[:4])[0]) ** 2 + (fp_center[1] - _rect_center(m[:4])[1]) ** 2) ** 0.5)
            for m in x_marks
        )
        foreign_partner = fp_dist <= _MAX_MARK_DISTANCE_PT

    return value, foreign_partner, evidence


def _tail_lines(text: str) -> list[str]:
    # The footer marker repeats once per page on a multi-page PDF (every
    # page carries the same "Cat. No. 10231X / Form W-9 (Rev. 3-2024)"
    # footer). The flattened values sit right after the FIRST occurrence
    # (page 1, where the fillable fields live) -- using the LAST occurrence
    # would land on the final page's footer instead and capture nothing but
    # unrelated instruction text from every page in between.
    start = text.find(_FOOTER_MARKER)
    if start == -1:
        return []
    start += len(_FOOTER_MARKER)
    end = text.find(_FOOTER_MARKER, start)  # stop before page 2's footer, if any
    tail = text[start:end] if end != -1 else text[start:]
    return [ln.strip() for ln in tail.splitlines() if ln.strip()]


def looks_like_flattened_tail(text: str) -> bool:
    lines = _tail_lines(text)
    if len(lines) < 4:
        return False
    return any(_EIN_RE.match(ln) or _SSN_RE.match(ln) for ln in lines)


def extract_from_flattened_tail(text: str, page=None) -> ExtractedFields | None:
    """`page` (a pymupdf Page, optional) enables tax-classification detection
    via checkbox-widget position matching -- see module docstring. Without
    it, tax classification is reported as genuinely unknown rather than
    guessed, same as before."""
    lines = _tail_lines(text)
    if len(lines) < 4:
        return None  # not enough content to be this pattern

    def ev(value):
        return _HIGH if value else _MISSING

    # Anchors: TIN (format-recognizable anywhere) and city/state/zip (shape-recognizable).
    tin_idx, tin_type, tin_raw = None, None, None
    csz_idx, csz_match = None, None
    date_idx = None
    for i, line in enumerate(lines):
        if tin_idx is None and _EIN_RE.match(line):
            tin_idx, tin_type, tin_raw = i, "EIN", line
        elif tin_idx is None and _SSN_RE.match(line):
            tin_idx, tin_type, tin_raw = i, "SSN", line
        if csz_idx is None:
            m = _CITY_STATE_ZIP_RE.match(line)
            if m:
                csz_idx, csz_match = i, m
        if date_idx is None and _DATE_RE.match(line):
            date_idx = i

    if csz_idx is None:
        return None  # can't orient without at least the address anchor

    legal_name = lines[0] if len(lines) > 0 else None
    dba_name = lines[1] if csz_idx >= 3 else None  # only trust a DBA slot if there's room before the address
    street = lines[csz_idx - 1] if csz_idx >= 1 else None

    signer = None
    if date_idx is not None and date_idx > 0:
        candidate = lines[date_idx - 1]
        # Don't mistake the TIN or a stray label line for the signer's name.
        if candidate not in (tin_raw,) and not _EIN_RE.match(candidate) and not _SSN_RE.match(candidate):
            signer = candidate

    tin = (
        TinInfo(type=tin_type, value_masked=mask_tin(tin_raw), value_token=tokenize_tin(tin_raw),
                format_valid=True, confidence=_HIGH, source=SOURCE)
        if tin_raw else
        TinInfo(type=None, value_masked=None, value_token=None, format_valid=False, confidence=_MISSING, source=SOURCE)
    )

    tc_value, tc_evidence, tc_confidence = None, "tax classification not determinable without page positional data", _MISSING
    llc_subclass = None
    foreign_partner_indicator = None
    if page is not None:
        tc_value, foreign_partner_indicator, tc_evidence = _detect_checked_classification(page)
        tc_confidence = _HIGH if tc_value else _MISSING
        if tc_value == "llc":
            # Observed pattern: when LLC is checked, the flattened text has a
            # standalone C/S/P line (the subclass entry) before the address --
            # unlike the checkbox mark itself, this IS a real, unambiguous label.
            for line in lines[:csz_idx]:
                if line in ("C", "S", "P"):
                    llc_subclass = line
                    break

    return ExtractedFields(
        legal_name=ExtractedValue(value=legal_name, confidence=ev(legal_name), source=SOURCE,
                                   evidence="flattened-tail position 0" if legal_name else None),
        dba_name=ExtractedValue(value=dba_name, confidence=ev(dba_name), source=SOURCE,
                                 evidence="flattened-tail position 1" if dba_name else None),
        tax_classification=TaxClassification(
            value=tc_value, confidence=tc_confidence, source=SOURCE, evidence=tc_evidence,
            llc_subclass=llc_subclass, foreign_partner_indicator=foreign_partner_indicator,
        ),
        address=Address(
            street=ExtractedValue(value=street, confidence=ev(street), source=SOURCE,
                                   evidence="flattened-tail, line before city/state/zip" if street else None),
            city=ExtractedValue(value=csz_match.group("city"), confidence=_HIGH, source=SOURCE, evidence=lines[csz_idx]),
            state=ExtractedValue(value=csz_match.group("state"), confidence=_HIGH, source=SOURCE, evidence=lines[csz_idx]),
            zip=ExtractedValue(value=csz_match.group("zip"), confidence=_HIGH, source=SOURCE, evidence=lines[csz_idx]),
        ),
        tin=tin,
        certification=Certification(
            signed=bool(signer) and date_idx is not None,
            signature_present=bool(signer),
            date=lines[date_idx] if date_idx is not None else None,
            confidence=_HIGH if signer else _MISSING,
        ),
    )
