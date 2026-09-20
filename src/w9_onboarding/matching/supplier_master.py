"""Loading and column contract for the (simulated) supplier master.

Columns, matching the case study's description of the existing supplier
master plus the token/hash fields our matcher actually needs:

  supplier_id, legal_name, dba_name, tin_token, tax_classification,
  address_street, address_city, address_state, address_zip,
  bank_routing_hash, bank_account_last4, bank_account_holder_name,
  status, last_updated_at

`tin_token` and `bank_routing_hash` stand in for "TIN (encrypted at rest)"
and "bank account (encrypted)" from the prompt: in production these would be
opaque references resolved by the encrypted supplier-master service itself,
never plaintext handed to this pipeline. The prototype derives them with the
same `security.py` helpers the extractor uses, so a TIN/routing number is
comparable across the two sides without either one ever storing or logging
the raw digits.
"""

from __future__ import annotations

import pandas as pd

REQUIRED_COLUMNS = [
    "supplier_id", "legal_name", "dba_name", "tin_token", "tax_classification",
    "address_street", "address_city", "address_state", "address_zip",
    "bank_routing_hash", "bank_account_last4", "bank_account_holder_name",
    "status", "last_updated_at",
]


def load_supplier_master(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    missing = set(REQUIRED_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"Supplier master is missing required columns: {sorted(missing)}")
    return df
