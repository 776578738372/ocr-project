"""Optional HTTP interface.

The primary, required entry point for this exercise is the CLI
(cli.py) -- the brief lists a production REST server under "what we are
not asking you to build" and says a CLI is sufficient. This module is
deliberately minimal (no auth, no persistence, no containers/deployment
config) and exists to show the same contract works over a plain upload
endpoint: it is a thin adapter with zero duplicated logic -- every request
calls the exact same `pipeline.run_pipeline` function the CLI calls, against
the same CSV-based supplier master the brief specifies for this exercise.

Run:
    pip install fastapi uvicorn python-multipart
    uvicorn w9_onboarding.api:app --reload --port 8000

Try it:
    curl -X POST http://localhost:8000/v1/w9/onboard \
      -F "tenant_id=tenant_pairsoft_042" \
      -F "file=@data/sample_w9s/clean_w9_acme.pdf"
"""

from __future__ import annotations

import json
import os

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from w9_onboarding.contracts.schema import SecondaryPayload, W9OnboardingResponse
from w9_onboarding.matching.supplier_master import load_supplier_master
from w9_onboarding.pipeline import run_pipeline

# One CSV per tenant under this directory, named "<tenant_id>.csv" -- the
# same CSV format the CLI takes via --supplier-master, just resolved by
# tenant_id instead of a path argument so the client doesn't need to
# re-upload the whole master on every request.
SUPPLIER_MASTER_DIR = os.environ.get("W9_SUPPLIER_MASTER_DIR", "data/tenants")

app = FastAPI(
    title="W-9 Onboarding Service",
    description="Optional HTTP interface over the same pipeline the CLI uses.",
    version="0.1.0",
)

_supplier_master_cache: dict[str, pd.DataFrame] = {}


def _get_supplier_master(tenant_id: str) -> pd.DataFrame:
    if tenant_id not in _supplier_master_cache:
        path = os.path.join(SUPPLIER_MASTER_DIR, f"{tenant_id}.csv")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail=f"No supplier master found for tenant '{tenant_id}'")
        _supplier_master_cache[tenant_id] = load_supplier_master(path)
    return _supplier_master_cache[tenant_id]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/w9/onboard", response_model=W9OnboardingResponse)
async def onboard_w9(
    tenant_id: str = Form(...),
    file: UploadFile = File(...),
    secondary_payload: str | None = Form(None),
) -> W9OnboardingResponse:
    file_bytes = await file.read()

    payload = None
    if secondary_payload:
        try:
            payload = SecondaryPayload.model_validate(json.loads(secondary_payload))
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid secondary_payload: {exc}") from exc

    supplier_df = _get_supplier_master(tenant_id)

    # Note: an invalid/corrupted document is NOT an HTTP error -- it's a
    # normal, documented contract outcome (decision.action == "REJECTED").
    # HTTP error codes here are reserved for things outside the contract:
    # an unknown tenant (404) or a malformed request (400/422).
    return run_pipeline(
        file_bytes=file_bytes,
        tenant_id=tenant_id,
        supplier_df=supplier_df,
        secondary_payload=payload,
    )
