"""STATUS: REAL (thin adapter). Optional HTTP interface.

The primary, required entry point for this exercise is the CLI (cli.py) --
the brief lists a production REST server under "what we are not asking you
to build" and says a CLI is sufficient. This module is deliberately minimal
(no auth, no persistence, no containers/deployment config) and exists to
show the same contract works over a plain upload endpoint: it is a thin
adapter with zero duplicated logic -- every request calls the exact same
`pipeline.run_pipeline` function the CLI calls, against the same CSV-based
supplier master the brief specifies for this exercise.

Multi-tenancy: this demo has one shared supplier master file
(data/supplier_master.csv) with a `tenant_id` column covering multiple
fictional tenants, rather than one file per tenant -- but isolation is
still real and enforced, just at a different layer. `_get_supplier_master`
loads the whole file; `decision/pipeline.py` filters it down to only the
requesting `tenant_id`'s rows *before* any candidate generation runs, so a
different tenant's supplier -- even one with an identical name -- is never
in the candidate pool at all (see tests/test_decision.py's
`tenant_isolation_same_name_different_tenant` case). Swapping this file for
a real supplier-master query API scoped by `tenant_id` is a change to
`_get_supplier_master` alone -- nothing else needs to know the difference.

Run from the repo root (--app-dir puts src/ on the import path without
needing to cd into it, which matters since decision/schemas/matching/etc.
are flat top-level packages under src/, not nested under a "src" package):

    pip install fastapi uvicorn python-multipart python-dotenv
    uvicorn main:app --app-dir src --reload --port 8000

A .env file in the repo root (if present) is loaded automatically below, so
ANTHROPIC_API_KEY / OPENAI_API_KEY set there activate the real VLM path
without needing `export` in whatever shell happens to start uvicorn --
this bit us once already (server started in a terminal that never ran
`export`, silently fell back to the mock, every image field showed "not
found" in the demo UI even though the same key worked fine from a shell
that had exported it).

Try it:
    curl -X POST http://localhost:8000/v1/w9/onboard \
      -F "tenant_id=tenant_pairsoft_042" \
      -F "file=@samples/w9_supplier_1_typed.pdf"
"""

from __future__ import annotations

import json
import os

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

load_dotenv()

from decision.pipeline import run_pipeline
from matching.supplier_master import load_supplier_master
from schemas.schema import SecondaryPayload, W9OnboardingResponse

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUPPLIER_MASTER_PATH = os.environ.get(
    "W9_SUPPLIER_MASTER_PATH", os.path.join(_REPO_ROOT, "data", "supplier_master.csv")
)
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(
    title="W-9 Onboarding Service",
    description="Optional HTTP interface over the same pipeline the CLI uses.",
    version="0.1.0",
)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Optional demo UI -- same-origin fetch to /v1/w9/onboard below, so it
    just works with `uvicorn main:app` and no CORS/mixed-content setup. Not
    part of the case study deliverable (the brief doesn't ask for a UI);
    the CLI remains the primary, required interface."""
    with open(os.path.join(_STATIC_DIR, "index.html")) as f:
        return f.read()

_supplier_master_cache: pd.DataFrame | None = None


def _get_supplier_master() -> pd.DataFrame:
    global _supplier_master_cache
    if _supplier_master_cache is None:
        if not os.path.exists(SUPPLIER_MASTER_PATH):
            raise HTTPException(status_code=500, detail=f"Supplier master not found at '{SUPPLIER_MASTER_PATH}'")
        _supplier_master_cache = load_supplier_master(SUPPLIER_MASTER_PATH)
    return _supplier_master_cache


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

    # Note: an invalid/corrupted document is NOT an HTTP error -- it's a
    # normal, documented contract outcome (decision.action == "REJECTED").
    # HTTP error codes here are reserved for things outside the contract
    # (a malformed request -> 400/422, or a missing supplier master -> 500).
    return run_pipeline(
        file_bytes=file_bytes,
        tenant_id=tenant_id,
        supplier_df=_get_supplier_master(),
        secondary_payload=payload,
    )
