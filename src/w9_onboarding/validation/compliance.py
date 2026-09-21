"""MOCK: TIN Matching and OFAC/sanctions screening providers.

There is no real IRS TIN Matching or OFAC/sanctions-screening integration in
this prototype -- both providers below honestly report "not checked" rather
than fabricating a match/no-match result. In production these would be real
calls to the IRS e-Services Bulk TIN Matching API and an OFAC/sanctions
screening vendor (see design_doc.md §6/§8). This module exists so that
integration point is visible in code -- wired into validation/rules.py below
-- rather than only described in prose.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TinMatchResult:
    checked: bool
    matched: bool | None  # None: not checked, not "no" -- these are different things
    provider: str


@dataclass(frozen=True)
class OfacScreeningResult:
    checked: bool
    hit: bool | None
    provider: str


class MockTinMatchingProvider:
    """MOCK: stands in for the IRS e-Services Bulk TIN Matching API."""

    def check(self, legal_name: str | None, tin_value_token: str | None) -> TinMatchResult:
        return TinMatchResult(
            checked=False, matched=None,
            provider="mock_tin_matching_v0 (no live IRS integration configured)",
        )


class MockOfacScreeningProvider:
    """MOCK: stands in for an OFAC/sanctions-list screening vendor."""

    def screen(self, legal_name: str | None) -> OfacScreeningResult:
        return OfacScreeningResult(
            checked=False, hit=None,
            provider="mock_ofac_screening_v0 (no live sanctions-list integration configured)",
        )
