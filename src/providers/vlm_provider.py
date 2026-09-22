"""STATUS: MIXED -- MockVLMExtractor (MOCK) is the active default;
AnthropicVLMExtractor and OpenAIVLMExtractor (REAL) exist and activate
automatically based on which API key is set. [2B] VLM_EXTRACTION:
vision-model extraction for scans, phone photos, and any PDF without a
usable embedded text layer.

Three implementations behind the same interface:

- MockVLMExtractor (the default, no key set): no API key required, so the
  pipeline runs end-to-end out of the box. It does NOT fabricate
  plausible-looking field values -- it honestly reports every field as
  unknown (confidence 0.0), which is the correct behavior for "the
  extractor this document needs isn't configured" rather than pretending to
  have read content it never saw. That low confidence flows into
  validation_flags and naturally forces ROUTE_TO_HUMAN_REVIEW downstream.
- AnthropicVLMExtractor: Claude's vision + tool use. Activates if
  ANTHROPIC_API_KEY is set.
- OpenAIVLMExtractor: GPT-4o's vision + function calling. Activates if
  ANTHROPIC_API_KEY is unset but OPENAI_API_KEY is set. Same field schema,
  same output shape -- get_vlm_extractor() picks whichever key is present
  and the rest of the pipeline never knows which provider actually ran.
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

_DEFAULT_ANTHROPIC_MODEL = os.environ.get("W9_VLM_MODEL", "claude-sonnet-5")
_DEFAULT_OPENAI_MODEL = os.environ.get("W9_OPENAI_VLM_MODEL", "gpt-4o")

_EXTRACTION_PROMPT = (
    "Extract every field from this IRS Form W-9 image. For each "
    "field, give a confidence 0-1 reflecting how legible/certain the "
    "source text is, and a short quoted evidence snippet where asked. "
    "If a field is genuinely absent or illegible, return null and 0.0."
)

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


def _image_media_type(file_bytes: bytes) -> str:
    return "image/png" if file_bytes[:8].startswith(b"\x89PNG") else "image/jpeg"


def _to_extracted_fields(data: dict) -> ExtractedFields:
    """Shared mapper: both providers return the same _FIELD_SCHEMA shape, so
    the JSON-to-ExtractedFields conversion only needs to exist once."""
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


class AnthropicVLMExtractor:
    source = SOURCE

    def __init__(self, model: str = _DEFAULT_ANTHROPIC_MODEL):
        import anthropic  # local import: optional dependency

        self._client = anthropic.Anthropic()
        self._model = model

    def extract(self, file_bytes: bytes) -> ExtractedFields:
        image_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            tools=[_FIELD_SCHEMA],
            tool_choice={"type": "tool", "name": "record_w9_fields"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": _image_media_type(file_bytes), "data": image_b64}},
                    {"type": "text", "text": _EXTRACTION_PROMPT},
                ],
            }],
        )

        tool_use = next(b for b in response.content if b.type == "tool_use")
        return _to_extracted_fields(tool_use.input)


class OpenAIVLMExtractor:
    source = SOURCE

    def __init__(self, model: str = _DEFAULT_OPENAI_MODEL):
        import openai  # local import: optional dependency

        self._client = openai.OpenAI()
        self._model = model

    def extract(self, file_bytes: bytes) -> ExtractedFields:
        image_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
        media_type = _image_media_type(file_bytes)

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": _EXTRACTION_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
                ],
            }],
            tools=[{
                "type": "function",
                "function": {
                    "name": _FIELD_SCHEMA["name"],
                    "description": _FIELD_SCHEMA["description"],
                    "parameters": _FIELD_SCHEMA["input_schema"],
                },
            }],
            tool_choice={"type": "function", "function": {"name": _FIELD_SCHEMA["name"]}},
        )

        tool_call = response.choices[0].message.tool_calls[0]
        data = json.loads(tool_call.function.arguments)
        return _to_extracted_fields(data)


def get_vlm_extractor():
    """ANTHROPIC_API_KEY wins if both are set (arbitrary but stable choice);
    otherwise OPENAI_API_KEY; otherwise the honest mock."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicVLMExtractor()
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIVLMExtractor()
    return MockVLMExtractor()
