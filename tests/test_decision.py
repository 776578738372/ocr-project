"""STATUS: REAL. End-to-end scenario coverage for the decision engine.

Real documents, not synthetic fixtures: 3 genuine W-9 PDFs (a "flattened"
fillable-PDF pattern -- see extraction/flattened_form.py) plus 4 real
photographed/handwritten W-9s requiring live Azure Document Intelligence
extraction, against data/evaluation_cases.json. Three scenarios, matching
how a real supplier master behaves: documents that match an existing
supplier cleanly, documents for a supplier not on file at all, and
documents for an existing supplier whose address changed since the record
was created (one PDF case and two image cases cover this last scenario).

The 4 scan/photo cases are marked `"requires_document_intelligence": true` in
the manifest and SKIP (not fail) when AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT /
AZURE_DOCUMENT_INTELLIGENCE_KEY aren't set -- consistent with the rest of
this project never requiring a paid API key just to run the test suite. Set
both env vars to actually exercise live extraction.

This is the answer to the case study's "some form of evaluation, even a
lightweight one" ask: run `pytest tests/test_decision.py -v` for the
decision-level pass/fail per scenario, or see test_field_level_accuracy for
the aggregate extraction accuracy metric.
"""

from __future__ import annotations

import json
import os

import pytest

from decision.pipeline import run_pipeline
from matching.supplier_master import load_supplier_master
from schemas.schema import SecondaryPayload

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES_PATH = os.path.join(REPO_ROOT, "data", "evaluation_cases.json")
SUPPLIER_MASTER_PATH = os.path.join(REPO_ROOT, "data", "supplier_master.csv")

HAS_AZURE_DI = bool(
    os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
    and os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_KEY")
)

with open(CASES_PATH) as _f:
    CASES = json.load(_f)


def _resolve_field_path(extracted_fields, path: str):
    cur = extracted_fields
    for part in path.split("."):
        cur = getattr(cur, part)
    if hasattr(cur, "value") and hasattr(cur, "confidence") and hasattr(cur, "source"):
        return cur.value
    return cur


def _run_case(case: dict):
    supplier_df = load_supplier_master(SUPPLIER_MASTER_PATH)

    with open(os.path.join(REPO_ROOT, case["file"]), "rb") as f:
        file_bytes = f.read()

    secondary_payload = None
    if case.get("secondary_payload"):
        with open(os.path.join(REPO_ROOT, case["secondary_payload"])) as f:
            secondary_payload = SecondaryPayload.model_validate(json.load(f))

    return run_pipeline(
        file_bytes=file_bytes,
        tenant_id=case.get("tenant_id", "tenant_pairsoft_042"),
        supplier_df=supplier_df,
        secondary_payload=secondary_payload,
    )


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_scenario_decision(case):
    if case.get("requires_document_intelligence") and not HAS_AZURE_DI:
        pytest.skip("no AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT/KEY set -- this case needs live extraction")

    response = _run_case(case)
    expected = case["expected"]

    if "decision_action" in expected:
        assert response.decision.action.value == expected["decision_action"]

    if "matched_supplier_id" in expected:
        assert response.decision.matched_supplier_id == expected["matched_supplier_id"]

    if "extraction_path" in expected:
        assert response.processing_metadata.extraction_path.value == expected["extraction_path"]

    if "max_candidates_evaluated" in expected:
        # Proves tenant isolation: the candidate pool size bounds how many
        # OTHER tenants' rows could possibly have leaked in. If this tenant
        # has 2 suppliers total, candidates_evaluated can never exceed 2 --
        # regardless of how many rows a different tenant has in the same CSV.
        assert response.match_evidence is not None
        assert response.match_evidence.candidates_evaluated <= expected["max_candidates_evaluated"]

    for code in expected.get("validation_flag_codes_include", []):
        assert any(f.code == code for f in response.validation_flags), (
            f"{code} not found in validation_flags for scenario '{case['name']}'"
        )

    for code in expected.get("decision_reasoning_codes_include", []):
        assert any(r.code == code for r in response.decision.decision_reasoning), (
            f"{code} not found in decision_reasoning for scenario '{case['name']}'"
        )

    for path, expected_value in expected.get("extracted_fields", {}).items():
        assert response.extracted_fields is not None, f"no extracted_fields for scenario '{case['name']}'"
        actual = _resolve_field_path(response.extracted_fields, path)
        assert actual == expected_value, (
            f"field '{path}' in scenario '{case['name']}': expected {expected_value!r}, got {actual!r}"
        )


def test_field_level_accuracy_report(capsys):
    """Aggregate field-level extraction accuracy across all scenarios --
    prints a summary rather than asserting per-field (test_scenario_decision
    already asserts those); this is the aggregate metric for the eval story.
    """
    total, passed = 0, 0
    for case in CASES:
        if case.get("requires_document_intelligence") and not HAS_AZURE_DI:
            continue
        response = _run_case(case)
        if response.extracted_fields is None:
            continue
        for path, expected_value in case["expected"].get("extracted_fields", {}).items():
            total += 1
            if _resolve_field_path(response.extracted_fields, path) == expected_value:
                passed += 1

    with capsys.disabled():
        pct = 100.0 * passed / total if total else 0.0
        print(f"\nField-level extraction accuracy: {passed}/{total} ({pct:.1f}%)")

    assert passed == total
