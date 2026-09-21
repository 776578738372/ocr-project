"""STATUS: REAL. The FSM itself: [0] UNINIT -> ... -> [7] TERMINATED.

This function is the one place that walks every state in the diagram. Each
module it calls (classifier, extractors, validation, matching) is a pure
function/class with no knowledge of the FSM -- this is the only place their
outputs get assembled into a state transition and, eventually, the contract
response.
"""

from __future__ import annotations

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
from providers.vlm_provider import MockVLMExtractor, get_vlm_extractor
from extraction.classifier import InvalidFileError, classify
from matching.scoring import match_supplier
from validation.rules import has_blocking_flag, validate

_COST_LAYOUT_OCR = 0.0015  # compute-only estimate, no external API call
_COST_VLM_LIVE = 0.02  # representative vision-model call cost
_COST_VLM_MOCK = 0.0  # no call actually made


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
        extractor = LayoutOCRExtractor()
        engine_versions["extractor"] = "layout-ocr-1.0"
        extraction_cost = _COST_LAYOUT_OCR
    else:
        state_trace.append("VLM_EXTRACTION")
        extractor = get_vlm_extractor()
        is_mock = isinstance(extractor, MockVLMExtractor)
        engine_versions["extractor"] = "vlm-mock-1.0" if is_mock else f"vlm-anthropic-{extractor._model}"
        extraction_cost = _COST_VLM_MOCK if is_mock else _COST_VLM_LIVE

    fields = extractor.extract(file_bytes)

    state_trace.append("NORMALIZED")
    flags = validate(fields)

    state_trace += ["KYS_CHECKED", "TIN_SEARCHED"]
    match_result = match_supplier(fields, supplier_df, secondary_payload)
    if match_result.match_evidence.tin_match:
        state_trace.append("EVAL_CHANGE")

    action = match_result.action
    matched_supplier_id = match_result.matched_supplier_id
    reasoning = list(match_result.decision_reasoning)

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
