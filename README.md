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
│   ├── supplier_master.csv        # sample supplier records (MOCK/sample data, real field shapes)
│   └── evaluation_cases.json      # ground truth for tests/test_decision.py
│
├── samples/                  # real W-9 documents (2 typed PDFs, 2 photos, 2 handwritten) -- see below
│
├── scripts/
│   └── generate_sample_data.py    # generates synthetic fixtures (not currently used -- see note below)
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
3. **[`tests/test_decision.py`](tests/test_decision.py)** — run this to see six real, non-synthetic
   W-9 documents (2 typed PDFs, 2 phone photos, 2 handwritten) resolve to the three outcomes a real
   supplier master needs: a document that matches an existing supplier cleanly, one for a supplier
   not on file at all, and one for an existing supplier whose address changed.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .                          # installs extraction/validation/matching/etc. as packages
```

`samples/` and `data/supplier_master.csv` are checked into the repo directly (real W-9 documents
aren't something a script can honestly fabricate) rather than generated on demand.
`scripts/generate_sample_data.py` still exists and works — it produces a fully synthetic fixture set
(clean match / new supplier / TIN hijack / unsigned / wrong-form / invalid-file / scanned-wrapper
scenarios) — but isn't wired into the current `samples/`/`data/evaluation_cases.json`; see "What I
deferred" below for why that coverage isn't currently re-attached to real test cases.

## Run it (primary entry point: the CLI)

```bash
# A clean match against the sample supplier master
python src/cli.py \
  --file samples/w9_supplier_1_typed.pdf \
  --tenant-id tenant_pairsoft_042 \
  --supplier-master data/supplier_master.csv

# A supplier not on file at all -> CREATE_NEW
python src/cli.py \
  --file samples/w9_supplier_3.jpeg \
  --tenant-id tenant_pairsoft_042 \
  --supplier-master data/supplier_master.csv
```

Prints the full JSON contract to stdout. Try any file under `samples/` — see
`data/evaluation_cases.json` for what each one should produce.

## Evaluate

```bash
python -m pytest -v                                    # everything, verbose
python -m pytest tests/test_decision.py -v              # the 6 end-to-end scenarios specifically
```

`tests/test_decision.py` is parametrized over `data/evaluation_cases.json` and also reports
aggregate field-level extraction accuracy — this is the answer to "some form of evaluation, even a
lightweight one." The 2 PDF-based cases always run (no API key needed); the 4 photo/handwritten
cases are marked `"requires_vlm_key": true` and **skip cleanly** (not fail) without a live vision
API key, so the suite still runs end to end with zero setup burden either way.

## Activating the real vision-model path (optional)

The scanned/photo path (`[2B] VLM_EXTRACTION`, `src/providers/vlm_provider.py`) defaults to
`MockVLMExtractor`, which honestly reports every field as unknown (confidence 0.0) rather than
fabricating plausible-looking values. Two real providers exist behind the same interface — set
either key and it activates automatically, no code changes:

```bash
export ANTHROPIC_API_KEY=sk-...    # Claude vision, or:
export OPENAI_API_KEY=sk-...       # GPT-4o vision
```

`ANTHROPIC_API_KEY` wins if both are set. Both were tested live against the 4 real photo/handwritten
samples in `samples/`; see "What I'd build next" for a real, honest finding from that testing (vision
extraction isn't perfectly reliable, and the design already accounts for that).

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
  -F "file=@samples/w9_supplier_1_typed.pdf"
```

An invalid/corrupted document still returns HTTP 200 with `decision.action: "REJECTED"`, since
that's a documented contract outcome, not a server error; HTTP error codes are reserved for things
outside the contract (malformed request → 400/422). Verified end-to-end against all 6 real sample
documents (both extraction paths, all three match outcomes) — results identical to the CLI.

**A tiny demo UI** (`src/static/index.html`) is served at `GET /` on the same app — upload a file,
see the decision, extracted fields, reasoning, and validation flags rendered nicely, with the raw
JSON available too. Same-origin fetch to `/v1/w9/onboard`, so no CORS/HTTPS setup needed; just open
`http://localhost:8000/` once `uvicorn` is running. Not part of the case study deliverable (no UI
was asked for) — purely a convenience for demoing the API without curl/Postman. Verified end-to-end
in a real browser (Playwright) across all three decision-outcome colors (create/update, review,
reject).

## What's real vs. mock — the honest breakdown

| Layer | Status | Detail |
|---|---|---|
| Classifier, layout-OCR extraction | **REAL** | `src/extraction/` — deterministic, no external calls. Verified against 2 real typed W-9 PDFs (a "flattened" fillable-PDF pattern — see `extraction/flattened_form.py`), 100% field accuracy. |
| Vision-model extraction (`[2B]`) | **MIXED, both real providers tested live** | `src/providers/vlm_provider.py` — `MockVLMExtractor` is the active default absent a key (honest "unknown", not fake data); `AnthropicVLMExtractor` and `OpenAIVLMExtractor` are both real and were both tested against 4 real photo/handwritten W-9s. |
| Validation rules | **REAL** | `src/validation/rules.py` — format/consistency checks, plus the mock-compliance wiring below. |
| Matching (blocking + scoring + overrides) | **REAL** | `src/matching/` — genuine `rapidfuzz` similarity, TIN-token matching, banking-change override. |
| TIN Matching / OFAC screening | **MOCK, wired for real** | `src/providers/compliance_provider.py` — no live IRS/sanctions integration; every response honestly reports "not checked" rather than omitting the check or faking clean. |
| Supplier master data | **MOCK/sample** | `data/supplier_master.csv` — sample records shaped to exercise the 3 match outcomes against the real sample documents; not a real customer's data. |
| API contract, security handling (TIN masking/tokenization) | **REAL** | `src/schemas/schema.py`, `src/utils/security.py` — the latter uses SHA-256 as a stand-in for a KMS-backed vault, documented as such. |
| CLI + HTTP interface | **REAL** | Both call the identical `run_pipeline`; verified giving identical output for the same input. |
| IRS TIN Matching / OFAC / address-verification *implementations*, KMS/vault, auth, persistence | **Not built** | Design-only — see `docs/design_doc.md` §6/§8/§9 for the integration points and reasoning. |

## Design decisions and tradeoffs

- **Two extraction engines, chosen deterministically at ingestion, not by trial-and-error.**
  `[1] INGESTED` runs a 3-stage probe (file format → PyMuPDF embedded-text check → text-density
  fallback) to route each document to `[2A]` deterministic layout parsing or `[2B]` a vision model.
  See `src/extraction/classifier.py`.
- **`[2A] LAYOUT_OCR` is real, not mocked, and has three sources tried in order — all found by
  testing against real documents, not designed in advance.** (1) AcroForm widget fields
  (`extraction/acroform.py`) for a genuinely fillable PDF where typed values live in form fields, not
  the text stream. (2) A "flattened" pattern (`extraction/flattened_form.py`) found on real documents
  where the widgets are empty but the values got baked into the text stream anyway, appended after the
  page footer, disconnected from their labels — including a multi-page-specific bug where the footer
  marker repeats once per page, so naively taking the *last* occurrence lands on the wrong page (fixed:
  take the first). (3) Text-stream regexes anchored on field labels, for flattened/non-fillable PDFs
  where labels and values ARE adjacent. A related bug: `looks_like_this_template()` originally matched
  on AcroForm field *names* alone — a flattened PDF can have the right field names with every value
  empty, which would wrongly commit to reading nothing instead of falling through to a path that could
  actually find the data. See `tests/test_extraction.py`'s regression tests against the real files.
- **Confidence is derived, not self-reported.** For layout OCR, confidence reflects whether a label
  anchor matched and the value passed its own format check — not a model's opinion of itself.
- **TIN never travels in plaintext past extraction.** `src/utils/security.py` is the one place raw
  digits are touched; everywhere else (validation, matching, decision, output) works with a masked
  value or a derived token. Matching against the supplier master compares tokens, never raw TINs.
- **Tenant isolation is real code, not just a documented intent** — `src/decision/pipeline.py`
  filters the supplier master to the requesting `tenant_id`'s rows *before* candidate generation
  runs, unconditionally, regardless of what data is loaded. This was previously demonstrated with a
  deliberate cross-tenant name collision (two different tenants each having an unrelated company
  named "Acme Corporation," proving a request never sees the other tenant's same-named supplier even
  at a perfect 1.0 name-similarity score); that specific fixture isn't in the current sample set
  after resetting `data/supplier_master.csv` around the new real-document scenarios — see "what I
  deferred" below.
- **A CRITICAL validation flag can override a match decision.** This is an addition on top of the
  FSM's match-driven routing, not a replacement for it: even a clean supplier match shouldn't be
  auto-applied against a document that's unsigned, has no valid TIN, or looks like the wrong form
  entirely (`src/validation/rules.py:BLOCKING_CODES`).
- **Banking/routing changes always force human review**, independent of name-match confidence — see
  `src/matching/scoring.py`. A high name-similarity score should never be able to paper over a
  changed payment destination.
- **A separate, tenant-wide check catches "multiple vendors, one bank account" — the case study's
  own named fraud pattern that per-supplier matching structurally can't see.** `src/matching/risk_signals.py`
  scans every supplier this tenant already has, not just the one a document matched (or didn't):
  a brand-new, otherwise-clean "new supplier" claiming a bank account already on file for a
  *different* existing supplier is forced to human review. Each fake vendor looks individually
  clean to the matcher; only a tenant-wide account scan catches the pattern. (Same caveat as tenant
  isolation above: the code is real and unchanged, but the specific fixture demonstrating it isn't
  in the current sample set — see "what I deferred.")
- **TIN Matching / OFAC screening are mock providers wired into the real pipeline, not just prose.**
  `src/providers/compliance_provider.py` has no live IRS/sanctions integration — it honestly reports
  "not checked" (`INFO_TIN_MATCHING_NOT_PERFORMED` / `INFO_OFAC_SCREENING_NOT_PERFORMED`) on every
  response rather than omitting the check or faking a clean result. Same pattern as
  `MockVLMExtractor`: swapping in a real provider is a new class behind the same interface.
- **Two independent real VLM providers, not one, behind the same interface.** `AnthropicVLMExtractor`
  and `OpenAIVLMExtractor` both implement the same tool/function-calling extraction against the same
  field schema; `get_vlm_extractor()` picks whichever API key is present. Both were tested live
  against the same 4 real photo/handwritten documents — useful in practice, since it surfaced that
  vision extraction accuracy depends heavily on image quality (overlapping/cluttered source images
  produced inconsistent, sometimes-wrong names across repeated calls on the same image; cleaner
  source images extracted reliably) — a real, worth-knowing limitation of the whole VLM path, not
  specific to either provider.
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
- **Exhaustive edge-case coverage.** Six scenarios (across 3 outcome types, on real documents) were
  kept as the current regression set, not exhaustive fuzzing of malformed input.
- **Re-attaching the synthetic edge-case fixtures** (invalid file, wrong-form-entirely, unsigned form,
  TIN hijack, cross-tenant name collision, shared-bank-account fraud) to the current sample set.
  `scripts/generate_sample_data.py` still generates all of these and the underlying pipeline code for
  every one is untouched and real (`validation/rules.py`'s `BLOCKING_CODES`, `matching/scoring.py`'s
  TIN-hijack branch, `decision/pipeline.py`'s tenant filter, `matching/risk_signals.py`) — they were
  deliberately not re-wired into `data/evaluation_cases.json` when `samples/`/`data/supplier_master.csv`
  were reset around the new real documents, since blending synthetic and real fixtures in one pass
  risked conflating "does the code still work" with "does it work on real input." Worth doing as a
  follow-up, not skipped as unimportant.

## What I'd build next with another 8 hours

1. Re-attach the synthetic edge-case fixtures (invalid file, wrong-form, unsigned, TIN hijack,
   tenant isolation, shared-bank-account) alongside the real documents, so both "does the code
   still work" and "does it work on real input" are covered by one regression suite.
2. Extend the banking/high-risk-change override to the no-TIN-match fuzzy-match path too, so a
   fuzzy-matched update gets the same fraud check as a TIN-matched one.
3. A recorded-response (cassette-style) fixture for the VLM providers so the 4 photo/handwritten
   test cases run deterministically in CI without a live, paid API call every time.
4. Push blocking/candidate generation down to a simulated "supplier master search API" boundary
   (rather than an in-memory pandas filter) to make the tenant-scale story concrete in code, not
   just in the design doc.
5. A confidence-calibration check in the eval harness: deliberately degrade a few sample images
   (blur, rotate, partial redaction) and confirm confidence scores actually drop where they should
   — directly motivated by this session's finding that image quality measurably affects VLM accuracy.
6. Wire `estimated_cost_usd` into a per-tenant running total, since the design doc's cost model
   assumes it's aggregated somewhere, not just reported per-request.
