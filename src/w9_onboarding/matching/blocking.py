"""[4] KYS_CHECKED --(SEARCH_MASTER)--> [5] TIN_SEARCHED: candidate generation.

A tenant's supplier master can hold tens of thousands of records, so the
matcher never fuzzy-compares against the whole table. Candidate generation is
the union of:
  - an exact TIN-token lookup (the strongest possible signal -- effectively
    an indexed point lookup in production), and
  - a blocked fuzzy search: normalize the legal name (strip case, punctuation,
    common business suffixes) and take a short prefix as a blocking key, then
    only consider supplier-master rows sharing that key.

In the prototype this runs as an in-memory pandas filter, which is fine at
sample scale. At real tenant scale, both predicates should be pushed down to
the supplier master service's own indexed search API (the prompt says we
have query/search access to it) rather than pulled into this service and
scanned locally.
"""

from __future__ import annotations

import re

import pandas as pd

_BUSINESS_SUFFIXES = (
    "CORPORATION", "INCORPORATED", "COMPANY", "ENTERPRISES", "HOLDINGS",
    "GROUP", "CORP", "INC", "LLC", "LLP", "LP", "LTD", "CO",
)
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]")
_WHITESPACE_RE = re.compile(r"\s+")

MAX_CANDIDATES = 50


def normalize_business_name(name: str | None) -> str:
    if not name:
        return ""
    normalized = _NON_ALNUM_RE.sub(" ", name.upper())
    for suffix in _BUSINESS_SUFFIXES:
        normalized = re.sub(rf"\b{suffix}\b", " ", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def blocking_key(name: str | None, key_len: int = 4) -> str:
    normalized = normalize_business_name(name)
    return normalized.replace(" ", "")[:key_len]


def generate_candidates(
    tin_token: str | None,
    legal_name: str | None,
    supplier_df: pd.DataFrame,
) -> pd.DataFrame:
    frames = []

    if tin_token:
        frames.append(supplier_df[supplier_df["tin_token"] == tin_token])

    key = blocking_key(legal_name)
    if key:
        name_keys = supplier_df["legal_name"].apply(blocking_key)
        dba_keys = supplier_df["dba_name"].apply(blocking_key)
        frames.append(supplier_df[(name_keys == key) | (dba_keys == key)])

    if not frames:
        return supplier_df.iloc[0:0]

    combined = pd.concat(frames).drop_duplicates(subset="supplier_id")
    return combined.head(MAX_CANDIDATES)
