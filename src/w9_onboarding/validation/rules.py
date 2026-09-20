"""[3] NORMALIZED -> validation flags.

Two kinds of checks:
  - Format / cross-field consistency (state codes, TIN shape, checkbox
    exclusivity, LLC sub-classification presence) -- routine data-quality
    signal, mostly INFO/WARNING.
  - "Is this even usable" checks (no legal name, no TIN, unsigned, or a
    layout-OCR pass that found neither a name nor a TIN at all -- the classic
    sign someone sent a W-8 instead) -- CRITICAL, and BLOCKING_CODES below is
    what the pipeline consults to override any match decision and force
    human review rather than auto-creating or auto-updating a supplier
    record from fundamentally unusable data. This is an addition on top of
    the FSM's own match-driven routing, not a replacement for it.
"""

from __future__ import annotations

from w9_onboarding.contracts.schema import ExtractedFields, ExtractionPath, Severity, ValidationFlag

_VALID_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "PR",
}

LOW_CONFIDENCE_THRESHOLD = 0.5

BLOCKING_CODES = {
    "ERR_MISSING_LEGAL_NAME",
    "ERR_INVALID_TIN",
    "ERR_NOT_SIGNED",
    "ERR_POSSIBLE_WRONG_FORM",
}


def _flag(code: str, severity: Severity, message: str, field: str | None = None) -> ValidationFlag:
    return ValidationFlag(code=code, severity=severity, field=field, message=message)


def validate(fields: ExtractedFields) -> list[ValidationFlag]:
    flags: list[ValidationFlag] = []

    if not fields.legal_name.value:
        flags.append(_flag(
            "ERR_MISSING_LEGAL_NAME", Severity.CRITICAL,
            "Legal name (Line 1) could not be extracted.", "legal_name",
        ))

    if not fields.tin.format_valid or not fields.tin.type:
        flags.append(_flag(
            "ERR_INVALID_TIN", Severity.CRITICAL,
            "TIN is missing or does not match SSN/EIN format.", "tin",
        ))

    if not fields.certification.signed:
        flags.append(_flag(
            "ERR_NOT_SIGNED", Severity.CRITICAL,
            "Form is missing a signature and/or date; invalid for tax reporting.",
            "certification",
        ))

    # A layout-OCR pass with a real embedded text layer that still can't find
    # either a name or a TIN anchor is the classic sign of the wrong form
    # entirely (e.g. a W-8 sent by a foreign supplier).
    if (
        fields.legal_name.source == ExtractionPath.LAYOUT_OCR
        and fields.legal_name.confidence == 0.0
        and fields.tin.confidence == 0.0
    ):
        flags.append(_flag(
            "ERR_POSSIBLE_WRONG_FORM", Severity.CRITICAL,
            "Document has an embedded text layer but no W-9 field anchors matched; "
            "this may be the wrong form (e.g. a W-8) rather than a malformed W-9.",
        ))

    tc = fields.tax_classification
    if tc.value is None:
        flags.append(_flag(
            "WARN_TAX_CLASSIFICATION_UNCLEAR", Severity.WARNING,
            "No federal tax classification checkbox could be identified.",
            "tax_classification",
        ))
    elif tc.evidence and "multiple boxes checked" in tc.evidence:
        flags.append(_flag(
            "WARN_TAX_CLASSIFICATION_AMBIGUOUS", Severity.WARNING,
            "More than one federal tax classification box appears checked.",
            "tax_classification",
        ))
    elif tc.value == "llc" and not tc.llc_subclass:
        flags.append(_flag(
            "WARN_LLC_SUBCLASS_MISSING", Severity.WARNING,
            "LLC was selected but no C/S/P sub-classification was found.",
            "tax_classification.llc_subclass",
        ))

    state = fields.address.state.value
    if state and state.upper() not in _VALID_STATES:
        flags.append(_flag(
            "WARN_INVALID_STATE_CODE", Severity.WARNING,
            f"'{state}' is not a recognized US state/territory code.",
            "address.state",
        ))

    for field_name, extracted in (
        ("legal_name", fields.legal_name),
        ("tin", fields.tin),
    ):
        confidence = getattr(extracted, "confidence", None)
        if confidence is not None and 0.0 < confidence < LOW_CONFIDENCE_THRESHOLD:
            flags.append(_flag(
                "WARN_LOW_EXTRACTION_CONFIDENCE", Severity.WARNING,
                f"Confidence on '{field_name}' ({confidence:.2f}) is below the "
                f"{LOW_CONFIDENCE_THRESHOLD} review threshold.",
                field_name,
            ))

    return flags


def has_blocking_flag(flags: list[ValidationFlag]) -> bool:
    return any(f.code in BLOCKING_CODES for f in flags)
