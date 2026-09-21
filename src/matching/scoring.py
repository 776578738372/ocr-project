"""STATUS: REAL. [5] TIN_SEARCHED -> [5A] EVAL_CHANGE -> [6A]/[6B]/[6C].

Decision rules, exactly as specified in the state machine:

  TIN_MATCH_FOUND & NAME_SIMILAR (>=0.70)   -> [6B] MATCHED_EXIST / UPDATE_EXISTING
  TIN_MATCH_FOUND & NAME_MISMATCH (<0.70)   -> [6C] ESCALATED_HUMAN (possible TIN hijack)
  NO_TIN_MATCH  & FUZZY_HIGH  (>=0.85)      -> [6B] MATCHED_EXIST / UPDATE_EXISTING
  NO_TIN_MATCH  & NO_FUZZY_MATCH            -> [6A] RESOLVED_NEW / CREATE_NEW

Independent override, applied inside EVAL_CHANGE regardless of the name-match
band: if a `secondary_payload` carries banking details that differ from what
the matched supplier has on file, force [6C] ESCALATED_HUMAN with
WARN_HIGH_RISK_BANKING_CHANGE. A high name-similarity score should never be
able to paper over a changed payment destination -- that combination is the
signature of payment-redirection fraud, and a human should see it even when
everything else about the record looks clean.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from rapidfuzz import fuzz, utils as fuzz_utils

from schemas.schema import (
    DecisionAction,
    DecisionReason,
    ExtractedFields,
    FieldDiff,
    MatchEvidence,
    SecondaryPayload,
)
from matching.blocking import generate_candidates
from utils.security import hash_identifier

NAME_SIMILARITY_THRESHOLD = 0.70  # TIN-match branch (EVAL_CHANGE)
NO_TIN_FUZZY_HIGH_THRESHOLD = 0.85  # no-TIN branch: stricter, no TIN corroboration

_NAME_WEIGHT = 0.6
_ADDRESS_WEIGHT = 0.3
_TAX_CLASS_WEIGHT = 0.1


@dataclass
class MatchResult:
    match_evidence: MatchEvidence
    action: DecisionAction
    matched_supplier_id: str | None
    overall_match_confidence: float
    decision_reasoning: list[DecisionReason]


def _similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    # rapidfuzz does NOT lowercase/normalize by default -- without this
    # processor, "ACME CORPORATION INC" vs "Acme Corporation" (the case
    # study's own duplicate-detection example) scores ~0.24 instead of
    # ~0.89, purely from case differences. default_process lowercases,
    # strips punctuation, and collapses whitespace before comparing.
    return fuzz.token_sort_ratio(a, b, processor=fuzz_utils.default_process) / 100.0


def _address_similarity(fields: ExtractedFields, candidate: pd.Series) -> float:
    incoming = " ".join([
        fields.address.street.value or "", fields.address.city.value or "",
        fields.address.state.value or "", fields.address.zip.value or "",
    ])
    existing = " ".join([
        candidate["address_street"], candidate["address_city"],
        candidate["address_state"], candidate["address_zip"],
    ])
    return _similarity(incoming, existing)


def _composite_score(name_sim: float, address_sim: float, tax_class_match: bool) -> float:
    return (
        _NAME_WEIGHT * name_sim
        + _ADDRESS_WEIGHT * address_sim
        + _TAX_CLASS_WEIGHT * (1.0 if tax_class_match else 0.0)
    )


def _field_diffs(fields: ExtractedFields, candidate: pd.Series) -> list[FieldDiff]:
    diffs = []
    pairs = [
        ("address.street", candidate["address_street"], fields.address.street.value),
        ("address.city", candidate["address_city"], fields.address.city.value),
        ("address.state", candidate["address_state"], fields.address.state.value),
        ("address.zip", candidate["address_zip"], fields.address.zip.value),
        ("tax_classification", candidate["tax_classification"], fields.tax_classification.value),
    ]
    for field_name, existing, incoming in pairs:
        if incoming and existing and incoming.strip().lower() != existing.strip().lower():
            diffs.append(FieldDiff(field=field_name, existing=existing, incoming=incoming))
    return diffs


def _banking_changed(secondary_payload: SecondaryPayload | None, candidate: pd.Series) -> bool:
    if not secondary_payload or not secondary_payload.banking:
        return False
    on_file_routing_hash = candidate.get("bank_routing_hash", "")
    on_file_last4 = candidate.get("bank_account_last4", "")
    if not on_file_routing_hash:
        return False  # nothing on file to compare against
    incoming_routing_hash = hash_identifier(secondary_payload.banking.routing_number)
    return (
        incoming_routing_hash != on_file_routing_hash
        or secondary_payload.banking.account_number_last4 != on_file_last4
    )


def match_supplier(
    fields: ExtractedFields,
    supplier_df: pd.DataFrame,
    secondary_payload: SecondaryPayload | None = None,
) -> MatchResult:
    candidates = generate_candidates(fields.tin.value_token, fields.legal_name.value, supplier_df)

    tin_matches = candidates[candidates["tin_token"] == (fields.tin.value_token or "")]
    tin_match_found = fields.tin.value_token is not None and not tin_matches.empty

    if tin_match_found:
        candidate = tin_matches.iloc[0]
        name_similarity = _similarity(fields.legal_name.value, candidate["legal_name"])
        banking_changed = _banking_changed(secondary_payload, candidate)
        diffs = _field_diffs(fields, candidate)

        reasoning = [DecisionReason(
            code="RULE_TIN_EXACT_MATCH",
            message=f"Extracted TIN matches supplier {candidate['supplier_id']} on file.",
        )]

        if banking_changed:
            reasoning.append(DecisionReason(
                code="WARN_HIGH_RISK_BANKING_CHANGE",
                message="Routing/account details differ from record despite a TIN match; "
                        "forcing human review regardless of name similarity.",
            ))
            action = DecisionAction.ROUTE_TO_HUMAN_REVIEW
            confidence = name_similarity
        elif name_similarity >= NAME_SIMILARITY_THRESHOLD:
            reasoning.append(DecisionReason(
                code="RULE_NAME_SIMILARITY_HIGH",
                message=f"Name similarity {name_similarity:.2f} >= {NAME_SIMILARITY_THRESHOLD} threshold.",
            ))
            action = DecisionAction.UPDATE_EXISTING
            confidence = name_similarity
        else:
            reasoning.append(DecisionReason(
                code="WARN_NAME_MISMATCH_ON_TIN_MATCH",
                message=f"Name similarity {name_similarity:.2f} < {NAME_SIMILARITY_THRESHOLD} threshold "
                        "despite an exact TIN match; possible TIN hijack or identity mismatch.",
            ))
            action = DecisionAction.ROUTE_TO_HUMAN_REVIEW
            confidence = name_similarity

        return MatchResult(
            match_evidence=MatchEvidence(
                candidates_evaluated=len(candidates),
                tin_match=True,
                matched_supplier_id=candidate["supplier_id"],
                name_similarity_score=name_similarity,
                fuzzy_algorithm="token_sort_ratio",
                field_diffs=diffs,
                banking_change_detected=banking_changed,
            ),
            action=action,
            matched_supplier_id=candidate["supplier_id"] if action != DecisionAction.ROUTE_TO_HUMAN_REVIEW else None,
            overall_match_confidence=confidence,
            decision_reasoning=reasoning,
        )

    # No TIN match: fall back to composite fuzzy scoring across all candidates.
    best_candidate = None
    best_score = 0.0
    best_name_sim = 0.0
    for _, row in candidates.iterrows():
        name_sim = _similarity(fields.legal_name.value, row["legal_name"])
        dba_sim = _similarity(fields.legal_name.value, row["dba_name"])
        name_sim = max(name_sim, dba_sim)
        address_sim = _address_similarity(fields, row)
        tax_match = fields.tax_classification.value == row["tax_classification"]
        score = _composite_score(name_sim, address_sim, tax_match)
        if score > best_score:
            best_score, best_candidate, best_name_sim = score, row, name_sim

    if best_candidate is not None and best_score >= NO_TIN_FUZZY_HIGH_THRESHOLD:
        diffs = _field_diffs(fields, best_candidate)
        return MatchResult(
            match_evidence=MatchEvidence(
                candidates_evaluated=len(candidates),
                tin_match=False,
                matched_supplier_id=best_candidate["supplier_id"],
                name_similarity_score=best_name_sim,
                fuzzy_algorithm="token_sort_ratio (composite: name/address/tax_class)",
                field_diffs=diffs,
                banking_change_detected=False,
            ),
            action=DecisionAction.UPDATE_EXISTING,
            matched_supplier_id=best_candidate["supplier_id"],
            overall_match_confidence=best_score,
            decision_reasoning=[DecisionReason(
                code="RULE_FUZZY_MATCH_HIGH",
                message=f"No TIN match, but composite similarity {best_score:.2f} >= "
                        f"{NO_TIN_FUZZY_HIGH_THRESHOLD} threshold against supplier "
                        f"{best_candidate['supplier_id']}.",
            )],
        )

    return MatchResult(
        match_evidence=MatchEvidence(
            candidates_evaluated=len(candidates),
            tin_match=False,
            matched_supplier_id=None,
            name_similarity_score=best_name_sim if best_candidate is not None else None,
            fuzzy_algorithm="token_sort_ratio (composite: name/address/tax_class)",
            field_diffs=[],
            banking_change_detected=False,
        ),
        action=DecisionAction.CREATE_NEW,
        matched_supplier_id=None,
        overall_match_confidence=1.0 - best_score,
        decision_reasoning=[DecisionReason(
            code="RULE_NO_TIN_MATCH",
            message="No supplier record shares this TIN.",
        ), DecisionReason(
            code="RULE_FUZZY_BELOW_THRESHOLD",
            message=f"Best composite similarity {best_score:.2f} < {NO_TIN_FUZZY_HIGH_THRESHOLD} threshold; "
                    "treating as a new supplier.",
        )],
    )
