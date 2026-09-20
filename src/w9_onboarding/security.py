"""Centralized handling of sensitive fields (TIN, bank routing/account).

Every place in the pipeline that needs to compare or mask a TIN or bank
identifier goes through this module, so there is exactly one place that
ever sees the raw digits. Nothing here is a substitute for a real KMS/vault
in production -- see the design doc's security section -- but it keeps the
prototype honest: no plaintext TIN is ever placed in a log line, a matching
key, or an API response.
"""

from __future__ import annotations

import hashlib
import re

_DIGITS_RE = re.compile(r"\D")


def _digits_only(value: str) -> str:
    return _DIGITS_RE.sub("", value or "")


def hash_identifier(value: str) -> str:
    """Deterministic hash used as the matching key for TINs / routing numbers.

    A real deployment would use a keyed HMAC with a secret held in a KMS so the
    hash can't be brute-forced offline (TINs/routing numbers are low-entropy).
    Plain sha256 is used here to keep the prototype runnable without a secrets
    store; the design doc calls out the HMAC upgrade explicitly.
    """
    digits = _digits_only(value)
    return hashlib.sha256(digits.encode("utf-8")).hexdigest()


def classify_tin_type(value: str) -> str | None:
    digits = _digits_only(value)
    if len(digits) != 9:
        return None
    # EIN and SSN are both 9 digits; the form's own field tells us which box
    # was checked, but as a fallback we can't distinguish by digits alone.
    return None


def mask_tin(value: str) -> str:
    digits = _digits_only(value)
    if len(digits) < 4:
        return "xx-xxxxxxx"
    return f"xx-xxx{digits[-4:]}"


def tokenize_tin(value: str) -> str:
    """Opaque reference a downstream, authorized service could resolve.

    In production this is a call to a tokenization vault; here it's a stable
    derived id so the same TIN always maps to the same token within a run.
    """
    return f"tin_tok_{hash_identifier(value)[:12]}"
