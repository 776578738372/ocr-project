# W-9 Extraction and Supplier Onboarding — Prototype

A slice of the design in [`docs/design_doc.md`](docs/design_doc.md): ingest a W-9 (PDF/PNG/JPEG),
extract its fields with a per-field confidence signal, match it against a supplier master, and emit
a structured decision (`CREATE_NEW` / `UPDATE_EXISTING` / `ROUTE_TO_HUMAN_REVIEW` / `REJECTED`).

## What to look at first

1. **`src/w9_onboarding/contracts/schema.py`** — the API contract. Everything else produces or
   consumes these types.
2. **`src/w9_onboarding/pipeline.py`** — the state machine itself (`[0] UNINIT` → `[7] TERMINATED`).
   Read this next; it calls everything else in order.
3. **`eval/run_eval.py`** + **`eval/manifest.json`** — run this to see nine real scenarios (clean
   match, new supplier, TIN hijack, unsigned form, wrong form, invalid file, scanned photo, scanned
   PDF wrapper, banking-change override) each produce the correct decision.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .                          # installs the w9_onboarding package
python3 scripts/generate_sample_data.py   # writes data/sample_supplier_master.csv + data/sample_w9s/
```

## Run it

```bash
# A clean match against the sample supplier master
python -m w9_onboarding.cli \
  --file data/sample_w9s/clean_w9_acme.pdf \
  --tenant-id tenant_pairsoft_042 \
  --supplier-master data/sample_supplier_master.csv

# With a secondary payload (banking details) to exercise the fraud-override path
python -m w9_onboarding.cli \
  --file data/sample_w9s/clean_w9_acme.pdf \
  --tenant-id tenant_pairsoft_042 \
  --supplier-master data/sample_supplier_master.csv \
  --secondary-payload data/sample_w9s/secondary_payload_banking_change.json
```

Prints the full JSON contract to stdout. Try any file under `data/sample_w9s/` — each one exercises
a different FSM path (see `eval/manifest.json` for the full list with expected outcomes).

## Evaluate

```bash
python eval/run_eval.py   # 9 scenarios, decision-level + field-level checks, plain-text report
python -m pytest -q       # 18 directional unit tests: classifier routing, validation rules, matching logic
```

## Activating the real vision-model path (optional)

The scanned/photo path (`[2B] VLM_EXTRACTION`) defaults to `MockVLMExtractor`, which honestly
reports every field as unknown (confidence 0.0) rather than fabricating plausible-looking values —
see the extractor's docstring. Set `ANTHROPIC_API_KEY` and it switches to a real Claude vision call
automatically, no code changes:

```bash
export ANTHROPIC_API_KEY=sk-...
pip install anthropic
```

## Design decisions and tradeoffs

- **Two extraction engines, chosen deterministically at ingestion, not by trial-and-error.**
  `[1] INGESTED` runs a 3-stage probe (file format → PyMuPDF embedded-text check → text-density
  fallback) to route each document to `[2A]` deterministic layout parsing or `[2B]` a vision model.
  See `ingestion/classifier.py`.
- **`[2A] LAYOUT_OCR` is real, not mocked.** For a digital PDF with an embedded text layer, PyMuPDF
  pulls the actual text and regexes anchored on the W-9's own field labels locate each field. This
  only runs on documents the classifier has already confirmed have genuine embedded text — anything
  else (scans, photos, wrong-form-entirely) goes to `[2B]` instead.
- **Confidence is derived, not self-reported.** For layout OCR, confidence reflects whether a label
  anchor matched and the value passed its own format check — not a model's opinion of itself.
- **TIN never travels in plaintext past extraction.** `security.py` is the one place raw digits are
  touched; everywhere else (validation, matching, decision, output) works with a masked value or a
  derived token. Matching against the supplier master compares tokens, never raw TINs.
- **A CRITICAL validation flag can override a match decision.** This is an addition on top of the
  FSM's match-driven routing, not a replacement for it: even a clean supplier match shouldn't be
  auto-applied against a document that's unsigned, has no valid TIN, or looks like the wrong form
  entirely (`validation/rules.py:BLOCKING_CODES`).
- **Banking/routing changes always force human review**, independent of name-match confidence — see
  `matching/scoring.py`. A high name-similarity score should never be able to paper over a changed
  payment destination.

## What I deferred, and why

- **Exemption codes (Line 4) and account numbers (Line 7).** The case study's own reference material
  notes these are "usually blank for typical business suppliers." Rendered in the sample PDFs for
  visual realism but not parsed — low value for the time budget.
- **A trained document-layout model for `[2A]`** (e.g. a custom Azure Document Intelligence model).
  The regex-anchored PyMuPDF approach is real, working extraction for digital PDFs, but a genuinely
  hand-marked or oddly-formatted digital PDF could defeat the label anchors. A layout model is the
  natural upgrade path once there's labeled review-outcome data to train one — see design doc §3.
- **IRS TIN Matching, OFAC/sanctions screening, USPS address verification.** All are real API calls
  in production (design doc §5 covers the integration points and where they gate the decision); the
  prototype validates format/consistency locally rather than mocking three more external services.
- **Applying the banking-change override to the no-TIN-match fuzzy path.** The FSM as specified
  scopes `[5A] EVAL_CHANGE` (and its banking check) to the TIN-match branch only. Extending it
  uniformly is a one-line change (see "next 8 hours" below) but wasn't in the spec, so I didn't
  silently add scope.
- **Exhaustive edge-case coverage.** Nine scenarios were chosen to hit each terminal state at least
  once via a distinct path, not to exhaustively fuzz malformed input.

## What I'd build next with another 8 hours

1. Extend the banking/high-risk-change override to the no-TIN-match fuzzy-match path too, so a
   fuzzy-matched update gets the same fraud check as a TIN-matched one.
2. A real `pip install`-able fixture for the AnthropicVLMExtractor path with a recorded response
   (cassette-style) so the VLM path has an automated test that doesn't require a live API key.
3. Push blocking/candidate generation down to a simulated "supplier master search API" boundary
   (rather than an in-memory pandas filter) to make the tenant-scale story concrete in code, not
   just in the design doc.
4. A confidence-calibration check in the eval harness: deliberately degrade a few sample PDFs (blur,
   partial redaction) and confirm confidence scores actually drop where they should.
5. Wire `estimated_cost_usd` into a per-tenant running total, since the design doc's cost model
   assumes it's aggregated somewhere, not just reported per-request.
