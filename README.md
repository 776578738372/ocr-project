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
│   ├── providers/                 # pluggable REAL/MOCK integrations (Document Intelligence, TIN/OFAC)
│   ├── schemas/                   # the API contract (pydantic) -- source of truth
│   └── utils/                     # security.py: the only module touching raw TINs
│
├── data/
│   ├── supplier_master.csv        # sample supplier records (MOCK/sample data, real field shapes)
│   └── evaluation_cases.json      # ground truth for tests/test_decision.py
│
├── samples/                  # real W-9 documents (3 typed PDFs, 2 photos, 2 handwritten) -- see below
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
3. **[`tests/test_decision.py`](tests/test_decision.py)** — run this to see seven real, non-synthetic
   W-9 documents (3 typed PDFs, 2 phone photos, 2 handwritten) resolve to the three outcomes a real
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
python -m pytest tests/test_decision.py -v              # the 7 end-to-end scenarios specifically
```

`tests/test_decision.py` is parametrized over `data/evaluation_cases.json` and also reports
aggregate field-level extraction accuracy — this is the answer to "some form of evaluation, even a
lightweight one." The 3 PDF-based cases always run (no credentials needed); the 4 photo/handwritten
cases are marked `"requires_document_intelligence": true` and **skip cleanly** (not fail) without
Azure Document Intelligence configured, so the suite still runs end to end with zero setup burden
either way.

## Activating scan/photo extraction (required for image-format documents)

A digital PDF with a real embedded text layer always works with zero setup — `[2A] OCR_EXTRACTION`
(`src/extraction/layout_ocr.py`) is deterministic, local, and free. Scans, phone photos, and any PDF
without a usable text layer (`[2B] VLM_EXTRACTION`) require Azure AI Document Intelligence:

```bash
export AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT=https://<your-resource>.cognitiveservices.azure.com/
export AZURE_DOCUMENT_INTELLIGENCE_KEY=...
```

Without these set, an image-format document raises a clear `AzureNotConfiguredError` (CLI: a one-line
`error:` message and exit code 1; HTTP: a 500) rather than silently returning nothing or crashing with
a traceback — see `src/decision/pipeline.py`.

**Why Document Intelligence, not a vision-LLM provider.** An earlier version of this prototype used
Claude/GPT-4o vision for this path. Live-testing against the real photo/handwritten samples in
`samples/` found a genuine accuracy gap: GPT-4o read a checked "Partnership" box as "C Corporation"
with self-reported confidence 1.0 — caught only because it produced a `field_diff` against that
supplier's existing record, which wouldn't have happened for a brand-new supplier (see
`docs/design_doc.md` §3.3 for the full writeup). A general vision-language model reasons about the
whole image at once and has no dedicated mechanism for checkbox state. Azure Document Intelligence's
`prebuilt-layout` model does: selection marks are a first-class, purpose-built model output (a
bounding polygon plus a selected/unselected state), not a language model's guess. This also unifies
extraction onto one engine capable of both PDF and image input, rather than maintaining two unrelated
code paths for the same problem — see `src/providers/document_intelligence_provider.py`'s docstring.
`data/evaluation_cases.json`'s `handwritten_supplier_exists_info_changed_2` case asserts the fixed
classification explicitly, as the regression test for this.

For the CLI, `export` both variables in the same shell before running it. For the HTTP interface
(`src/main.py`), a `.env` file in the repo root is loaded automatically (`python-dotenv`) — this
matters in practice: credentials only `export`ed in one terminal are invisible to a `uvicorn` server
started from a different one. A `.env` file works everywhere regardless of which shell started which
process.

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
| Classifier, layout-OCR extraction | **REAL** | `src/extraction/` — deterministic, no external calls. Verified against 3 real typed W-9 PDFs (a "flattened" fillable-PDF pattern — see `extraction/flattened_form.py`), 100% field accuracy. |
| Scan/photo extraction (`[2B]`), and `[2A]` when Azure is configured | **REAL, live-verified** | `src/providers/document_intelligence_provider.py` — Azure Document Intelligence's `prebuilt-layout` model; no fallback if unconfigured (raises `AzureNotConfiguredError` instead of faking data). Replaced an earlier Claude/GPT-4o vision-LLM implementation after live testing found it unreliable on checkbox state specifically, then live-tested itself against all 7 real documents — see `docs/design_doc.md` §3.3 for both rounds of findings, including the regression test proving the original checkbox-accuracy bug is actually fixed. |
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
  fallback) to route each document to `[2A]` deterministic layout parsing or `[2B]` Azure Document
  Intelligence.
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
- **Tax classification on the flattened pattern is resolved by checkbox position, not guessed.** The
  flattened text has only a lone "X" with no label next to it — which of the 7 boxes it belongs to
  can't be read from text alone. But the empty AcroForm widgets (that's *why* this fallback runs) still
  have correct `.rect` positions, so the "X" word's bounding box (from `page.get_text("words")`) can be
  matched to whichever checkbox widget is nearest. This caught a real case where guessing from the
  entity name would have been wrong: "Global Tech Solutions **Inc.**" reads as an obvious C-corp guess,
  but the box actually checked on the document is S-corp — see
  `tests/test_extraction.py::test_flattened_tail_checkbox_position_beats_guessing_from_entity_name`.
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
  response rather than omitting the check or faking a clean result.
- **The scan/photo extractor was replaced after live testing found a real accuracy gap, not
  designed this way from the start.** An earlier version used two vision-LLM providers
  (`AnthropicVLMExtractor`/`OpenAIVLMExtractor`) behind a `MockVLMExtractor`-backed interface —
  tested live against the same 4 real photo/handwritten documents. That testing surfaced two
  findings: extraction accuracy depended heavily on image quality (overlapping/cluttered source
  images produced inconsistent, sometimes-wrong names across repeated calls), and — more seriously —
  a model could self-report confidence 1.0 on a checkbox it read wrong (a checked "Partnership" box
  read as "C Corporation"), with no independent way to catch it for a brand-new supplier with no
  existing record to diff against. Both findings motivated the switch to Azure Document Intelligence
  (`src/providers/document_intelligence_provider.py`), whose selection-mark detection is a
  purpose-built model output rather than a language model's guess — see `docs/design_doc.md` §3.3,
  including a second round of findings from testing the replacement itself against live credentials
  (label-text/checkbox-matching/OCR-separator bugs, all fixed) and one genuine, honest limitation it
  surfaced rather than fixed.
- **Name-similarity scoring normalizes case/punctuation before comparing.** `rapidfuzz` does not do
  this by default: without it, the case study's own duplicate example ("ACME CORPORATION INC" vs
  "Acme Corporation") scored ~0.24 instead of ~0.89, which would have escalated a clean match as a
  possible TIN hijack. Fixed via `rapidfuzz.utils.default_process`; see `tests/test_matching.py`'s
  case-insensitivity regression test and `docs/design_doc.md` §4 for how this was found.

## What I deferred, and why

- **Exemption codes (Line 4) and account numbers (Line 7).** The case study's own reference material
  notes these are "usually blank for typical business suppliers." Rendered in the sample PDFs for
  visual realism but not parsed — low value for the time budget.
- **A custom-trained Document Intelligence model, vs. the prebuilt `prebuilt-layout` model actually
  used.** Both `[2A]` and `[2B]` route through Azure Document Intelligence when it's configured
  (`decision/pipeline.py` checks once, ahead of both branches) — but on the *prebuilt* layout model,
  with no W-9-specific training. `[2A]`'s original regex-anchored PyMuPDF approach
  (`extraction/layout_ocr.py`) still exists and remains the fallback when Azure isn't configured,
  since it's real, working, zero-cost extraction with no known accuracy gap on digital PDFs. A
  custom-trained model is the natural further upgrade once there's labeled review-outcome data to
  train one — see design doc §3.
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
3. A recorded-response (cassette-style) fixture for Azure Document Intelligence so the 4
   photo/handwritten test cases run deterministically in CI without a live, paid API call every time.
4. Push blocking/candidate generation down to a simulated "supplier master search API" boundary
   (rather than an in-memory pandas filter) to make the tenant-scale story concrete in code, not
   just in the design doc.
5. A confidence-calibration check in the eval harness: deliberately degrade a few sample images
   (blur, rotate, partial redaction) and confirm confidence scores actually drop where they should
   — directly motivated by this session's finding that image quality measurably affects extraction
   accuracy (first observed with the earlier vision-LLM providers, before the Document Intelligence
   switch).
6. Wire `estimated_cost_usd` into a per-tenant running total, since the design doc's cost model
   assumes it's aggregated somewhere, not just reported per-request.
