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
anchors. It does not attempt tax classification -- there is no reliable way
to tell which checkbox a lone flattened "X" belongs to without the bounding-
box/positional analysis this fallback deliberately keeps out of scope; that
gap surfaces honestly downstream as a normal "no checkbox marked" signal,
not a silent guess.
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


def extract_from_flattened_tail(text: str) -> ExtractedFields | None:
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

    return ExtractedFields(
        legal_name=ExtractedValue(value=legal_name, confidence=ev(legal_name), source=SOURCE,
                                   evidence="flattened-tail position 0" if legal_name else None),
        dba_name=ExtractedValue(value=dba_name, confidence=ev(dba_name), source=SOURCE,
                                 evidence="flattened-tail position 1" if dba_name else None),
        # Not attempted here -- see module docstring. Reported as genuinely
        # unknown, not guessed, consistent with this extractor's honesty bar.
        tax_classification=TaxClassification(
            value=None, confidence=_MISSING, source=SOURCE,
            evidence="tax classification not determinable from flattened text without positional/bounding-box analysis",
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
