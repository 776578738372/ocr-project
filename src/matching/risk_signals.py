"""STATUS: REAL. Tenant-wide fraud signals -- independent of, and run
alongside, the TIN/name matching in scoring.py.

scoring.py's banking-change check only ever compares against the ONE
supplier a document matched. This module answers a different question,
explicitly named in the case study's KYS section: "Multiple different
suppliers submitting the same bank account... are all red flags." That's a
classic payment-redirection pattern -- a fraudster registers several
differently-named "vendors" that all route to the same account -- and it's
invisible to per-supplier matching, since each fake vendor looks like a
clean, unrelated new supplier on its own. Catching it requires scanning
*every* supplier this tenant already has, not just the one that matched (or
didn't).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from schemas.schema import SecondaryPayload
from utils.security import hash_identifier


@dataclass(frozen=True)
class SharedBankAccountResult:
    shared: bool
    conflicting_supplier_id: str | None = None
    conflicting_legal_name: str | None = None


def check_shared_bank_account(
    secondary_payload: SecondaryPayload | None,
    tenant_supplier_df: pd.DataFrame,
    exclude_supplier_id: str | None = None,
) -> SharedBankAccountResult:
    """`tenant_supplier_df` must already be filtered to the requesting
    tenant (see decision/pipeline.py) -- this function does not re-check
    tenant_id itself, to keep isolation enforcement in one place.

    `exclude_supplier_id` is the supplier this document already matched, if
    any: reusing your OWN bank account on file is normal, not a signal --
    only a DIFFERENT supplier already having this exact account is.
    """
    if not secondary_payload or not secondary_payload.banking:
        return SharedBankAccountResult(shared=False)

    incoming_routing_hash = hash_identifier(secondary_payload.banking.routing_number)
    incoming_last4 = secondary_payload.banking.account_number_last4

    candidates = tenant_supplier_df[
        (tenant_supplier_df["bank_routing_hash"] == incoming_routing_hash)
        & (tenant_supplier_df["bank_account_last4"] == incoming_last4)
        & (tenant_supplier_df["bank_routing_hash"] != "")
    ]
    if exclude_supplier_id:
        candidates = candidates[candidates["supplier_id"] != exclude_supplier_id]

    if candidates.empty:
        return SharedBankAccountResult(shared=False)

    conflict = candidates.iloc[0]
    return SharedBankAccountResult(
        shared=True,
        conflicting_supplier_id=conflict["supplier_id"],
        conflicting_legal_name=conflict["legal_name"],
    )
