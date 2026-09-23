"""STATUS: REAL. The FSM itself: [0] UNINIT -> ... -> [7] TERMINATED.

This function is the one place that walks every state in the diagram. Each
module it calls (classifier, extractors, validation, matching) is a pure
function/class with no knowledge of the FSM -- this is the only place their
outputs get assembled into a state transition and, eventually, the contract
response.
"""

from __future__ import annotations

import os
import time
import uuid

import pandas as pd

from schemas.schema import (
    Decision,
    DecisionAction,
    DecisionReason,
    ExtractionPath,
    ProcessingMetadata,
    SecondaryPayload,
    Severity,
    ValidationFlag,
    W9OnboardingResponse,
)
from extraction.layout_ocr import LayoutOCRExtractor
from extraction.classifier import InvalidFileError, classify
from matching.risk_signals import check_shared_bank_account
from matching.scoring import match_supplier
from validation.rules import has_blocking_flag, validate

_COST_LAYOUT_OCR = 0.0015  # compute-only estimate, no external API call
_COST_AZURE_DI = 0.0015  # representative Document Intelligence prebuilt-layout call (~$1.50/1000 pages)


class AzureNotConfiguredError(RuntimeError):
    """Raised when a document needs [2B] VLM_EXTRACTION (a scan/photo, or a
    PDF with no embedded text layer) but AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT
    / AZURE_DOCUMENT_INTELLIGENCE_KEY aren't set. There is deliberately no
    other extractor for this state to fall back to -- see
    providers/document_intelligence_provider.py's docstring for why the
    vision-LLM providers this project used before were retired instead of
    kept as a fallback. This is a deployment/configuration problem, not a
    per-document outcome, so callers (cli.py, main.py) surface it as a clear
    error rather than routing it through the FSM's decision vocabulary."""


def _azure_di_configured() -> bool:
    return bool(
        os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
        and os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_KEY")
    )


def run_pipeline(
    file_bytes: bytes,
    tenant_id: str,
    supplier_df: pd.DataFrame,
    secondary_payload: SecondaryPayload | None = None,
    request_id: str | None = None,
) -> W9OnboardingResponse:
    start = time.monotonic()
    request_id = request_id or f"req_{uuid.uuid4().hex[:12]}"
    state_trace = ["UNINIT", "INGESTED"]

    try:
        classification = classify(file_bytes)
    except InvalidFileError as exc:
        state_trace += ["REJECTED_INVALID", "TERMINATED"]
        return W9OnboardingResponse(
            request_id=request_id,
            tenant_id=tenant_id,
            processing_time_ms=_elapsed_ms(start),
            processing_metadata=ProcessingMetadata(
                extraction_path=ExtractionPath.NOT_APPLICABLE,
                routing_signal="invalid_file",
                engine_versions={"classifier": "w9-classifier-1.0"},
                estimated_cost_usd=0.0,
                state_trace=state_trace,
            ),
            decision=Decision(
                action=DecisionAction.REJECTED,
                matched_supplier_id=None,
                overall_match_confidence=0.0,
                decision_reasoning=[DecisionReason(
                    code="ERR_INVALID_FILE_FORMAT", message=str(exc),
                )],
            ),
            validation_flags=[ValidationFlag(
                code="ERR_INVALID_FILE_FORMAT", severity=Severity.CRITICAL, message=str(exc),
            )],
        )

    engine_versions = {"classifier": "w9-classifier-1.0", "matching_rules": "kys-rules-1.0"}

    if classification.extraction_path == ExtractionPath.LAYOUT_OCR:
        state_trace.append("OCR_EXTRACTION")
        if _azure_di_configured():
            from providers.document_intelligence_provider import AzureDocumentIntelligenceExtractor

            extractor = AzureDocumentIntelligenceExtractor(source=ExtractionPath.LAYOUT_OCR)
            engine_versions["extractor"] = "azure-document-intelligence-prebuilt-layout"
            extraction_cost = _COST_AZURE_DI
        else:
            extractor = LayoutOCRExtractor()
            engine_versions["extractor"] = "layout-ocr-1.0"
            extraction_cost = _COST_LAYOUT_OCR
    else:
        state_trace.append("VLM_EXTRACTION")
        if not _azure_di_configured():
            raise AzureNotConfiguredError(
                "This document has no usable embedded text layer (a scan, photo, "
                "or image), which requires Azure Document Intelligence. Set "
                "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT and AZURE_DOCUMENT_INTELLIGENCE_KEY."
            )
        from providers.document_intelligence_provider import AzureDocumentIntelligenceExtractor

        extractor = AzureDocumentIntelligenceExtractor(source=ExtractionPath.VLM_EXTRACTION)
        engine_versions["extractor"] = "azure-document-intelligence-prebuilt-layout"
        extraction_cost = _COST_AZURE_DI

    fields = extractor.extract(file_bytes)

    state_trace.append("NORMALIZED")
    flags = validate(fields)

    state_trace += ["KYS_CHECKED", "TIN_SEARCHED"]
    # Tenant isolation, enforced here rather than trusted from the caller:
    # a different tenant's supplier -- even one with an identical name or
    # the exact same TIN -- is filtered out before candidate generation
    # ever sees it, not merely excluded by some later check that a caller
    # could forget to apply.
    tenant_supplier_df = supplier_df[supplier_df["tenant_id"] == tenant_id]
    match_result = match_supplier(fields, tenant_supplier_df, secondary_payload)
    if match_result.match_evidence.tin_match:
        state_trace.append("EVAL_CHANGE")

    action = match_result.action
    matched_supplier_id = match_result.matched_supplier_id
    reasoning = list(match_result.decision_reasoning)

    # Tenant-wide fraud signal, independent of whether THIS document matched
    # anything: does the incoming bank account already belong to a
    # DIFFERENT supplier this tenant has on file? Reusing your own account
    # (exclude_supplier_id) is normal; a different supplier already having
    # it is the "multiple vendors, one bank account" fraud pattern.
    shared_bank = check_shared_bank_account(secondary_payload, tenant_supplier_df, matched_supplier_id)
    if shared_bank.shared:
        action = DecisionAction.ROUTE_TO_HUMAN_REVIEW
        matched_supplier_id = None
        reasoning.append(DecisionReason(
            code="WARN_BANK_ACCOUNT_SHARED_ACROSS_SUPPLIERS",
            message=f"Incoming bank account is already on file for a different supplier "
                    f"({shared_bank.conflicting_supplier_id}: {shared_bank.conflicting_legal_name}); "
                    "possible multiple-vendors-one-account fraud pattern.",
        ))

    if has_blocking_flag(flags):
        action = DecisionAction.ROUTE_TO_HUMAN_REVIEW
        matched_supplier_id = None
        reasoning.append(DecisionReason(
            code="RULE_BLOCKING_VALIDATION_FLAG",
            message="One or more CRITICAL validation flags require human review before "
                    "any create/update is applied.",
        ))

    state_trace.append({
        DecisionAction.CREATE_NEW: "RESOLVED_NEW",
        DecisionAction.UPDATE_EXISTING: "MATCHED_EXIST",
        DecisionAction.ROUTE_TO_HUMAN_REVIEW: "ESCALATED_HUMAN",
    }[action])
    state_trace.append("TERMINATED")

    return W9OnboardingResponse(
        request_id=request_id,
        tenant_id=tenant_id,
        processing_time_ms=_elapsed_ms(start),
        processing_metadata=ProcessingMetadata(
            extraction_path=classification.extraction_path,
            routing_signal=classification.routing_signal,
            engine_versions=engine_versions,
            estimated_cost_usd=extraction_cost,
            state_trace=state_trace,
        ),
        extracted_fields=fields,
        match_evidence=match_result.match_evidence,
        decision=Decision(
            action=action,
            matched_supplier_id=matched_supplier_id,
            overall_match_confidence=match_result.overall_match_confidence,
            decision_reasoning=reasoning,
        ),
        validation_flags=flags,
    )


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
