"""Lightweight evaluation harness.

Not a test suite -- a runnable report answering "how do we know extraction
and matching are working?" against the scenario manifest in manifest.json.
Each scenario encodes a real FSM path (clean update, new supplier, TIN
hijack, unsigned form, wrong form, invalid file, scanned/mock VLM, banking
override) with the ground truth we know because we generated the sample
documents ourselves (see scripts/generate_sample_data.py).

Run: python eval/run_eval.py
Exits non-zero if any scenario's decision-level assertions fail, so it can
double as a CI smoke check even though that's not its primary purpose.
"""

from __future__ import annotations

import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from w9_onboarding.contracts.schema import SecondaryPayload
from w9_onboarding.matching.supplier_master import load_supplier_master
from w9_onboarding.pipeline import run_pipeline

MANIFEST_PATH = os.path.join(REPO_ROOT, "eval", "manifest.json")
SUPPLIER_MASTER_PATH = os.path.join(REPO_ROOT, "data", "sample_supplier_master.csv")


def resolve_field_path(extracted_fields, path: str):
    cur = extracted_fields
    for part in path.split("."):
        cur = getattr(cur, part)
    if hasattr(cur, "value") and hasattr(cur, "confidence") and hasattr(cur, "source"):
        return cur.value
    return cur


def run_case(case: dict, supplier_df) -> dict:
    file_path = os.path.join(REPO_ROOT, case["file"])
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    secondary_payload = None
    if case.get("secondary_payload"):
        with open(os.path.join(REPO_ROOT, case["secondary_payload"])) as f:
            secondary_payload = SecondaryPayload.model_validate(json.load(f))

    response = run_pipeline(
        file_bytes=file_bytes,
        tenant_id="tenant_pairsoft_eval",
        supplier_df=supplier_df,
        secondary_payload=secondary_payload,
    )

    checks: list[tuple[str, bool, str]] = []
    expected = case["expected"]

    if "decision_action" in expected:
        actual = response.decision.action.value
        ok = actual == expected["decision_action"]
        checks.append(("decision_action", ok, f"expected {expected['decision_action']!r}, got {actual!r}"))

    if "matched_supplier_id" in expected:
        actual = response.decision.matched_supplier_id
        ok = actual == expected["matched_supplier_id"]
        checks.append(("matched_supplier_id", ok, f"expected {expected['matched_supplier_id']!r}, got {actual!r}"))

    if "extraction_path" in expected:
        actual = response.processing_metadata.extraction_path.value
        ok = actual == expected["extraction_path"]
        checks.append(("extraction_path", ok, f"expected {expected['extraction_path']!r}, got {actual!r}"))

    for code in expected.get("validation_flag_codes_include", []):
        present = any(f.code == code for f in response.validation_flags)
        checks.append((f"validation_flag:{code}", present, "not found in validation_flags"))

    for code in expected.get("decision_reasoning_codes_include", []):
        present = any(r.code == code for r in response.decision.decision_reasoning)
        checks.append((f"decision_reasoning:{code}", present, "not found in decision_reasoning"))

    field_results = []
    if response.extracted_fields is not None:
        for path, expected_value in expected.get("extracted_fields", {}).items():
            actual_value = resolve_field_path(response.extracted_fields, path)
            ok = actual_value == expected_value
            field_results.append((path, ok, expected_value, actual_value))
            checks.append((f"field:{path}", ok, f"expected {expected_value!r}, got {actual_value!r}"))

    return {"case": case, "checks": checks, "field_results": field_results, "response": response}


def main() -> int:
    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)
    supplier_df = load_supplier_master(SUPPLIER_MASTER_PATH)

    results = [run_case(case, supplier_df) for case in manifest]

    total_field_checks = 0
    passed_field_checks = 0
    total_cases_passed = 0

    print("=" * 88)
    for r in results:
        name = r["case"]["name"]
        all_pass = all(ok for _, ok, _ in r["checks"])
        total_cases_passed += int(all_pass)
        status = "PASS" if all_pass else "FAIL"
        print(f"[{status}] {name}")
        print(f"        {r['case']['description']}")
        for check_name, ok, detail in r["checks"]:
            if not ok:
                print(f"        - FAILED: {check_name} ({detail})")
        for _, ok, _, _ in r["field_results"]:
            total_field_checks += 1
            passed_field_checks += int(ok)
        print()

    print("=" * 88)
    print(f"Scenarios: {total_cases_passed}/{len(results)} fully passed")
    if total_field_checks:
        pct = 100.0 * passed_field_checks / total_field_checks
        print(f"Field-level extraction accuracy: {passed_field_checks}/{total_field_checks} ({pct:.1f}%)")
    print("=" * 88)

    return 0 if total_cases_passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
