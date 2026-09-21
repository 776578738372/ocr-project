"""STATUS: MIXED -- MockVLMExtractor (MOCK) is the active default;
AnthropicVLMExtractor (REAL) exists and activates automatically if
ANTHROPIC_API_KEY is set, but is untested without a live key. [2B]
VLM_EXTRACTION: vision-model extraction for scans, phone photos, and any PDF
without a usable embedded text layer.

Two implementations behind the same interface:

- MockVLMExtractor (the default): no API key required, so the pipeline runs
  end-to-end out of the box. It does NOT fabricate plausible-looking field
  values -- it honestly reports every field as unknown (confidence 0.0),
  which is the correct behavior for "the extractor this document needs isn't
  configured" rather than pretending to have read content it never saw. That
  low confidence flows into validation_flags and naturally forces
  ROUTE_TO_HUMAN_REVIEW downstream.
- AnthropicVLMExtractor: a real implementation using Claude's vision + tool
  use, activated automatically the moment ANTHROPIC_API_KEY is set. No other
  code changes needed -- see extraction/__init__.py's get_vlm_extractor().
"""

from __future__ import annotations

import base64
import json
import os

from schemas.schema import (
    Address,
    Certification,
    ExtractedFields,
    ExtractedValue,
    ExtractionPath,
    TaxClassification,
    TinInfo,
)
from utils.security import mask_tin, tokenize_tin

SOURCE = ExtractionPath.VLM_EXTRACTION

_DEFAULT_MODEL = os.environ.get("W9_VLM_MODEL", "claude-sonnet-5")

_FIELD_SCHEMA = {
    "name": "record_w9_fields",
    "description": "Record the structured fields extracted from a W-9 image.",
    "input_schema": {
        "type": "object",
        "properties": {
            "legal_name": {"type": ["string", "null"]},
            "legal_name_confidence": {"type": "number"},
            "legal_name_evidence": {"type": ["string", "null"]},
            "dba_name": {"type": ["string", "null"]},
            "dba_name_confidence": {"type": "number"},
            "tax_classification": {
                "type": ["string", "null"],
                "enum": [
                    "individual_sole_proprietor", "c_corporation", "s_corporation",
                    "partnership", "trust_estate", "llc", "other", None,
                ],
            },
            "tax_classification_confidence": {"type": "number"},
            "llc_subclass": {"type": ["string", "null"]},
            "foreign_partner_indicator": {"type": ["boolean", "null"]},
            "street": {"type": ["string", "null"]},
            "street_confidence": {"type": "number"},
            "city": {"type": ["string", "null"]},
            "state": {"type": ["string", "null"]},
            "zip": {"type": ["string", "null"]},
            "address_confidence": {"type": "number"},
            "tin_type": {"type": ["string", "null"], "enum": ["SSN", "EIN", None]},
            "tin_value": {"type": ["string", "null"]},
            "tin_confidence": {"type": "number"},
            "signature_present": {"type": "boolean"},
            "certification_date": {"type": ["string", "null"]},
            "certification_confidence": {"type": "number"},
        },
        "required": ["legal_name", "tax_classification", "tin_type", "tin_value"],
    },
}


class MockVLMExtractor:
    source = SOURCE

    def extract(self, file_bytes: bytes) -> ExtractedFields:
        def unknown(evidence="vlm_mock: no live vision model configured (set ANTHROPIC_API_KEY to activate)"):
            return ExtractedValue(value=None, confidence=0.0, source=SOURCE, evidence=evidence)

        return ExtractedFields(
            legal_name=unknown(),
            dba_name=unknown(),
            tax_classification=TaxClassification(
                value=None, confidence=0.0, source=SOURCE,
                evidence="vlm_mock: no live vision model configured",
            ),
            address=Address(street=unknown(), city=unknown(), state=unknown(), zip=unknown()),
            tin=TinInfo(
                type=None, value_masked=None, value_token=None,
                format_valid=False, confidence=0.0, source=SOURCE,
            ),
            certification=Certification(
                signed=False, signature_present=False, date=None, confidence=0.0,
            ),
        )


class AnthropicVLMExtractor:
    source = SOURCE

    def __init__(self, model: str = _DEFAULT_MODEL):
        import anthropic  # local import: optional dependency

        self._client = anthropic.Anthropic()
        self._model = model

    def extract(self, file_bytes: bytes) -> ExtractedFields:
        media_type = "image/png" if file_bytes[:8].startswith(b"\x89PNG") else "image/jpeg"
        image_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            tools=[_FIELD_SCHEMA],
            tool_choice={"type": "tool", "name": "record_w9_fields"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                    {"type": "text", "text": (
                        "Extract every field from this IRS Form W-9 image. For each "
                        "field, give a confidence 0-1 reflecting how legible/certain the "
                        "source text is, and a short quoted evidence snippet where asked. "
                        "If a field is genuinely absent or illegible, return null and 0.0."
                    )},
                ],
            }],
        )

        tool_use = next(b for b in response.content if b.type == "tool_use")
        data = tool_use.input
        return self._to_extracted_fields(data)

    def _to_extracted_fields(self, data: dict) -> ExtractedFields:
        tin_value = data.get("tin_value")
        if tin_value:
            tin = TinInfo(
                type=data.get("tin_type"),
                value_masked=mask_tin(tin_value),
                value_token=tokenize_tin(tin_value),
                format_valid=True,
                confidence=float(data.get("tin_confidence", 0.0)),
                source=SOURCE,
            )
        else:
            tin = TinInfo(type=None, value_masked=None, value_token=None, format_valid=False, confidence=0.0, source=SOURCE)

        return ExtractedFields(
            legal_name=ExtractedValue(
                value=data.get("legal_name"), confidence=float(data.get("legal_name_confidence", 0.0)),
                source=SOURCE, evidence=data.get("legal_name_evidence"),
            ),
            dba_name=ExtractedValue(
                value=data.get("dba_name"), confidence=float(data.get("dba_name_confidence", 0.0)), source=SOURCE,
            ),
            tax_classification=TaxClassification(
                value=data.get("tax_classification"),
                confidence=float(data.get("tax_classification_confidence", 0.0)),
                source=SOURCE,
                llc_subclass=data.get("llc_subclass"),
                foreign_partner_indicator=data.get("foreign_partner_indicator"),
            ),
            address=Address(
                street=ExtractedValue(value=data.get("street"), confidence=float(data.get("street_confidence", 0.0)), source=SOURCE),
                city=ExtractedValue(value=data.get("city"), confidence=float(data.get("address_confidence", 0.0)), source=SOURCE),
                state=ExtractedValue(value=data.get("state"), confidence=float(data.get("address_confidence", 0.0)), source=SOURCE),
                zip=ExtractedValue(value=data.get("zip"), confidence=float(data.get("address_confidence", 0.0)), source=SOURCE),
            ),
            tin=tin,
            certification=Certification(
                signed=bool(data.get("signature_present")) and bool(data.get("certification_date")),
                signature_present=bool(data.get("signature_present")),
                date=data.get("certification_date"),
                confidence=float(data.get("certification_confidence", 0.0)),
            ),
        )


def get_vlm_extractor():
    """Returns the real extractor if ANTHROPIC_API_KEY is set, else the mock."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicVLMExtractor()
    return MockVLMExtractor()
