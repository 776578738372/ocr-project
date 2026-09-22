"""STATUS: REAL. Command-line entry point -- the primary, required interface
for this exercise (the brief lists a REST server under "not asking you to
build" and says a CLI is sufficient; see src/main.py for the additional,
optional HTTP interface).

Run from the repo root:
    python src/cli.py \\
        --file samples/w9_supplier_1_typed.pdf \\
        --tenant-id tenant_pairsoft_042 \\
        --supplier-master data/supplier_master.csv

Prints the full W9OnboardingResponse JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys

from schemas.schema import SecondaryPayload
from matching.supplier_master import load_supplier_master
from decision.pipeline import run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="W-9 extraction and supplier onboarding decision")
    parser.add_argument("--file", required=True, help="Path to the W-9 PDF/PNG/JPEG")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--supplier-master", required=True, help="Path to the supplier master CSV")
    parser.add_argument("--secondary-payload", default=None, help="Optional path to a JSON secondary payload (banking, etc.)")
    args = parser.parse_args(argv)

    with open(args.file, "rb") as f:
        file_bytes = f.read()

    supplier_df = load_supplier_master(args.supplier_master)

    secondary_payload = None
    if args.secondary_payload:
        with open(args.secondary_payload) as f:
            secondary_payload = SecondaryPayload.model_validate(json.load(f))

    response = run_pipeline(
        file_bytes=file_bytes,
        tenant_id=args.tenant_id,
        supplier_df=supplier_df,
        secondary_payload=secondary_payload,
    )

    print(response.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
