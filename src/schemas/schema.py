"""STATUS: REAL -- the source of truth every other module produces or
consumes. Pydantic models for the W-9 onboarding API contract.

This module is the single source of truth for the request/response shape.
Every other module produces or consumes these types rather than raw dicts,
so a schema change here is the only place the contract can drift.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

API_VERSION = "2026-09-01"

ExtractionSource = str  # "layout_ocr" | "vlm_extraction"


class DecisionAction(str, Enum):
    CREATE_NEW = "CREATE_NEW"
    UPDATE_EXISTING = "UPDATE_EXISTING"
    ROUTE_TO_HUMAN_REVIEW = "ROUTE_TO_HUMAN_REVIEW"
    REJECTED = "REJECTED"


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class ExtractionPath(str, Enum):
    LAYOUT_OCR = "layout_ocr"
    VLM_EXTRACTION = "vlm_extraction"
    NOT_APPLICABLE = "n/a"


# ---------------------------------------------------------------------------
# Secondary payload (optional input alongside the W-9 document)
# ---------------------------------------------------------------------------


class BankingInfo(BaseModel):
    routing_number: str
    account_number_last4: str
    account_holder_name: str


class SecondaryPayload(BaseModel):
    banking: Optional[BankingInfo] = None


# ---------------------------------------------------------------------------
# Extracted fields
# ---------------------------------------------------------------------------


class ExtractedValue(BaseModel):
    value: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    source: ExtractionPath
    evidence: Optional[str] = None


class TaxClassification(ExtractedValue):
    llc_subclass: Optional[str] = None  # "C" | "S" | "P", only when value == "llc"
    foreign_partner_indicator: Optional[bool] = None  # Line 3b, 2024 revision only
    other_description: Optional[str] = None


class Address(BaseModel):
    street: ExtractedValue
    city: ExtractedValue
    state: ExtractedValue
    zip: ExtractedValue


class TinInfo(BaseModel):
    type: Optional[str] = None  # "SSN" | "EIN"
    value_masked: Optional[str] = None
    value_token: Optional[str] = None
    format_valid: bool
    confidence: float = Field(ge=0.0, le=1.0)
    source: ExtractionPath


class Certification(BaseModel):
    signed: bool
    signature_present: bool
    date: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractedFields(BaseModel):
    legal_name: ExtractedValue
    dba_name: ExtractedValue
    tax_classification: TaxClassification
    address: Address
    tin: TinInfo
    certification: Certification


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


class FieldDiff(BaseModel):
    field: str
    existing: Optional[str] = None
    incoming: Optional[str] = None


class MatchEvidence(BaseModel):
    candidates_evaluated: int
    tin_match: bool
    matched_supplier_id: Optional[str] = None
    name_similarity_score: Optional[float] = None
    fuzzy_algorithm: Optional[str] = None
    field_diffs: list[FieldDiff] = Field(default_factory=list)
    banking_change_detected: bool = False


# ---------------------------------------------------------------------------
# Decision + validation
# ---------------------------------------------------------------------------


class DecisionReason(BaseModel):
    code: str
    message: str


class Decision(BaseModel):
    action: DecisionAction
    matched_supplier_id: Optional[str] = None
    overall_match_confidence: float = Field(ge=0.0, le=1.0)
    decision_reasoning: list[DecisionReason] = Field(default_factory=list)


class ValidationFlag(BaseModel):
    code: str
    severity: Severity
    field: Optional[str] = None
    message: str


class ProcessingMetadata(BaseModel):
    extraction_path: ExtractionPath
    routing_signal: str
    engine_versions: dict[str, str] = Field(default_factory=dict)
    estimated_cost_usd: float
    state_trace: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level response
# ---------------------------------------------------------------------------


class W9OnboardingResponse(BaseModel):
    api_version: str = API_VERSION
    request_id: str
    tenant_id: str
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    processing_time_ms: int

    processing_metadata: ProcessingMetadata
    extracted_fields: Optional[ExtractedFields] = None
    match_evidence: Optional[MatchEvidence] = None
    decision: Decision
    validation_flags: list[ValidationFlag] = Field(default_factory=list)
