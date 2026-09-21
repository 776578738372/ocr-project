# W-9 Extraction and Supplier Onboarding — Prototype

A slice of the design in [`docs/design_doc.md`](docs/design_doc.md): ingest a W-9 (PDF/PNG/JPEG),
extract its fields with a per-field confidence signal, match it against a supplier master, and emit
a structured decision (`CREATE_NEW` / `UPDATE_EXISTING` / `ROUTE_TO_HUMAN_REVIEW` / `REJECTED`).

## Project structure

```
.
├── README.md
├── requirements.txt
├── pyproject.toml
├── .gitignore
│
├── src/                      # flat top-level packages (no wrapper package name)
│   ├── main.py                   # optional HTTP interface (FastAPI) -- see below
│   ├── cli.py                     # PRIMARY entry point (the brief asks for a CLI)
│   ├── extraction/                # [1] routing + [2A] deterministic layout-OCR extraction
│   ├── validation/                # [3] format/consistency checks -> validation_flags
│   ├── matching/                  # [4]-[6] candidate generation, scoring, decision
│   ├── decision/                  # the FSM orchestrator (pipeline.py) tying it all together
│   ├── providers/                 # pluggable REAL/MOCK integrations (vision model, TIN/OFAC)
│   ├── schemas/                   # the API contract (pydantic) -- source of truth
│   └── utils/                     # security.py: the only module touching raw TINs
│
├── data/
│   ├── supplier_master.csv        # 3 fictional suppliers (MOCK/sample data)
│   └── evaluation_cases.json      # ground truth for tests/test_decision.py
│
├── samples/                  # sample W-9 documents (real files) + one example response
│
├── scripts/
│   └── generate_sample_data.py    # (re)generates data/ and samples/ from scratch
│
└── tests/
    ├── test_extraction.py         # classifier routing
    ├── test_matching.py           # blocking + scoring
    ├── test_validation.py         # format/consistency rules + mock compliance wiring
    └── test_decision.py           # end-to-end scenarios (folds in the eval harness)
```

Every module's docstring opens with `STATUS: REAL`, `STATUS: MOCK`, or `STATUS: MIXED` — see
"What's real vs. mock" below for the full breakdown.

## What to look at first

1. **[`src/schemas/schema.py`](src/schemas/schema.py)** — the API contract. Everything else
   produces or consumes these types.
2. **[`src/decision/pipeline.py`](src/decision/pipeline.py)** — the state machine itself
   (`[0] UNINIT` → `[7] TERMINATED`). Read this next; it calls everything else in order.
3. **[`tests/test_decision.py`](tests/test_decision.py)** — run this to see nine real scenarios
   (clean match, new supplier, TIN hijack, unsigned form, wrong form, invalid file, scanned photo,
   scanned PDF wrapper, banking-change override) each produce the correct decision.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .                          # installs extraction/validation/matching/etc. as packages
python3 scripts/generate_sample_data.py   # writes data/supplier_master.csv + samples/
```

## Run it (primary entry point: the CLI)

```bash
# A clean match against the sample supplier master
python src/cli.py \
  --file samples/clean_w9_acme.pdf \
  --tenant-id tenant_pairsoft_042 \
  --supplier-master data/supplier_master.csv

# With a secondary payload (banking details) to exercise the fraud-override path
python src/cli.py \
  --file samples/clean_w9_acme.pdf \
  --tenant-id tenant_pairsoft_042 \
  --supplier-master data/supplier_master.csv \
  --secondary-payload samples/secondary_payload_banking_change.json
```

Prints the full JSON contract to stdout — [`samples/sample_w9.json`](samples/sample_w9.json) is a
saved example of exactly this output. Try any file under `samples/` — each one exercises a
different FSM path (see `data/evaluation_cases.json` for the full list with expected outcomes).

## Evaluate

```bash
python -m pytest -v                                    # everything, verbose
python -m pytest tests/test_decision.py -v              # the 9 end-to-end scenarios specifically
```

`tests/test_decision.py` is parametrized over `data/evaluation_cases.json` and also reports
aggregate field-level extraction accuracy (currently 100%) — this is the answer to "some form of
evaluation, even a lightweight one."

## Activating the real vision-model path (optional)

The scanned/photo path (`[2B] VLM_EXTRACTION`, `src/providers/vlm_provider.py`) defaults to
`MockVLMExtractor`, which honestly reports every field as unknown (confidence 0.0) rather than
fabricating plausible-looking values. Set `ANTHROPIC_API_KEY` and it switches to a real Claude
vision call automatically, no code changes:

```bash
export ANTHROPIC_API_KEY=sk-...
pip install anthropic
```

## Optional HTTP interface

The CLI above is the primary entry point — the brief lists a production REST server under "what
we are not asking you to build," and a CLI is what it asks for. `src/main.py` is a minimal
addition on top of that (no auth, no persistence, no deployment config) showing the same contract
works over a plain upload endpoint. It's a thin adapter with zero duplicated logic: every request
calls the exact same `pipeline.run_pipeline` function the CLI calls, against the same
`data/supplier_master.csv` the CLI takes via `--supplier-master`.

```bash
pip install fastapi uvicorn python-multipart

# --app-dir puts src/ on the import path without needing to cd into it, since
# extraction/validation/matching/etc. are flat top-level packages under src/
uvicorn main:app --app-dir src --reload --port 8000

curl -X POST http://localhost:8000/v1/w9/onboard \
  -F "tenant_id=tenant_pairsoft_042" \
  -F "file=@samples/clean_w9_acme.pdf"
```

An invalid/corrupted document still returns HTTP 200 with `decision.action: "REJECTED"`, since
that's a documented contract outcome, not a server error; HTTP error codes are reserved for things
outside the contract (malformed request → 400/422). Verified end-to-end: clean match, new supplier,
TIN hijack, unsigned form, banking-change override, invalid file, and the scanned/VLM-mock path all
produce results identical to the CLI.

## What's real vs. mock — the honest breakdown

| Layer | Status | Detail |
|---|---|---|
| Classifier, layout-OCR extraction | **REAL** | `src/extraction/` — deterministic, no external calls, 100% field accuracy on the 9 eval samples. |
| Validation rules | **REAL** | `src/validation/rules.py` — format/consistency checks, plus the mock-compliance wiring below. |
| Matching (blocking + scoring + overrides) | **REAL** | `src/matching/` — genuine `rapidfuzz` similarity, TIN-token matching, banking-change override. |
| Vision-model extraction (`[2B]`) | **MIXED** | `src/providers/vlm_provider.py` — `MockVLMExtractor` is the active default (honest "unknown", not fake data); a real `AnthropicVLMExtractor` exists and activates via `ANTHROPIC_API_KEY`. |
| TIN Matching / OFAC screening | **MOCK, wired for real** | `src/providers/compliance_provider.py` — no live IRS/sanctions integration; every response honestly reports "not checked" rather than omitting the check or faking clean. |
| Supplier master data | **MOCK/sample** | `data/supplier_master.csv` — 3 fictional suppliers, realistic in shape, not real data. |
| API contract, security handling (TIN masking/tokenization) | **REAL** | `src/schemas/schema.py`, `src/utils/security.py` — the latter uses SHA-256 as a stand-in for a KMS-backed vault, documented as such. |
| CLI + HTTP interface | **REAL** | Both call the identical `run_pipeline`; verified giving identical output for the same input. |
| IRS TIN Matching / OFAC / address-verification *implementations*, KMS/vault, auth, persistence | **Not built** | Design-only — see `docs/design_doc.md` §6/§8/§9 for the integration points and reasoning. |

## Design decisions and tradeoffs

- **Two extraction engines, chosen deterministically at ingestion, not by trial-and-error.**
  `[1] INGESTED` runs a 3-stage probe (file format → PyMuPDF embedded-text check → text-density
  fallback) to route each document to `[2A]` deterministic layout parsing or `[2B]` a vision model.
  See `src/extraction/classifier.py`.
- **`[2A] LAYOUT_OCR` is real, not mocked.** For a digital PDF with an embedded text layer, PyMuPDF
  pulls the actual text and regexes anchored on the W-9's own field labels locate each field. This
  only runs on documents the classifier has already confirmed have genuine embedded text — anything
  else (scans, photos, wrong-form-entirely) goes to `[2B]` instead.
- **Confidence is derived, not self-reported.** For layout OCR, confidence reflects whether a label
  anchor matched and the value passed its own format check — not a model's opinion of itself.
- **TIN never travels in plaintext past extraction.** `src/utils/security.py` is the one place raw
  digits are touched; everywhere else (validation, matching, decision, output) works with a masked
  value or a derived token. Matching against the supplier master compares tokens, never raw TINs.
- **A CRITICAL validation flag can override a match decision.** This is an addition on top of the
  FSM's match-driven routing, not a replacement for it: even a clean supplier match shouldn't be
  auto-applied against a document that's unsigned, has no valid TIN, or looks like the wrong form
  entirely (`src/validation/rules.py:BLOCKING_CODES`).
- **Banking/routing changes always force human review**, independent of name-match confidence — see
  `src/matching/scoring.py`. A high name-similarity score should never be able to paper over a
  changed payment destination.
- **TIN Matching / OFAC screening are mock providers wired into the real pipeline, not just prose.**
  `src/providers/compliance_provider.py` has no live IRS/sanctions integration — it honestly reports
  "not checked" (`INFO_TIN_MATCHING_NOT_PERFORMED` / `INFO_OFAC_SCREENING_NOT_PERFORMED`) on every
  response rather than omitting the check or faking a clean result. Same pattern as
  `MockVLMExtractor`: swapping in a real provider is a new class behind the same interface.
- **Name-similarity scoring normalizes case/punctuation before comparing.** `rapidfuzz` does not do
  this by default: without it, the case study's own duplicate example ("ACME CORPORATION INC" vs
  "Acme Corporation") scored ~0.24 instead of ~0.89, which would have escalated a clean match as a
  possible TIN hijack. Fixed via `rapidfuzz.utils.default_process`; see `tests/test_matching.py`'s
  case-insensitivity regression test and `docs/design_doc.md` §4 for how this was found.

## What I deferred, and why

- **Exemption codes (Line 4) and account numbers (Line 7).** The case study's own reference material
  notes these are "usually blank for typical business suppliers." Rendered in the sample PDFs for
  visual realism but not parsed — low value for the time budget.
- **A trained document-layout model for `[2A]`** (e.g. a custom Azure Document Intelligence model).
  The regex-anchored PyMuPDF approach is real, working extraction for digital PDFs, but a genuinely
  hand-marked or oddly-formatted digital PDF could defeat the label anchors. A layout model is the
  natural upgrade path once there's labeled review-outcome data to train one — see design doc §3.
- **Real IRS TIN Matching, OFAC/sanctions screening integrations, and USPS address verification.**
  Mock providers for the first two are wired into the pipeline (see above); real integrations are
  design-only (design doc §6/§8 covers the integration points and where they'd gate the decision).
- **Applying the banking-change override to the no-TIN-match fuzzy path.** The FSM as specified
  scopes `[5A] EVAL_CHANGE` (and its banking check) to the TIN-match branch only. Extending it
  uniformly is a one-line change (see "next 8 hours" below) but wasn't in the spec, so I didn't
  silently add scope.
- **Exhaustive edge-case coverage.** Nine scenarios were chosen to hit each terminal state at least
  once via a distinct path, not to exhaustively fuzz malformed input.

## What I'd build next with another 8 hours

1. Extend the banking/high-risk-change override to the no-TIN-match fuzzy-match path too, so a
   fuzzy-matched update gets the same fraud check as a TIN-matched one.
2. A real `pip install`-able fixture for the `AnthropicVLMExtractor` path with a recorded response
   (cassette-style) so the VLM path has an automated test that doesn't require a live API key.
3. Push blocking/candidate generation down to a simulated "supplier master search API" boundary
   (rather than an in-memory pandas filter) to make the tenant-scale story concrete in code, not
   just in the design doc.
4. A confidence-calibration check in the eval harness: deliberately degrade a few sample PDFs (blur,
   partial redaction) and confirm confidence scores actually drop where they should.
5. Wire `estimated_cost_usd` into a per-tenant running total, since the design doc's cost model
   assumes it's aggregated somewhere, not just reported per-request.
