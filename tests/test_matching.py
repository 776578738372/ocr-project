import pandas as pd

from schemas.schema import (
    Address,
    BankingInfo,
    Certification,
    DecisionAction,
    ExtractedFields,
    ExtractedValue,
    ExtractionPath,
    SecondaryPayload,
    TaxClassification,
    TinInfo,
)
from matching.blocking import normalize_business_name
from matching.risk_signals import check_shared_bank_account
from matching.scoring import match_supplier
from utils.security import hash_identifier, tokenize_tin

SOURCE = ExtractionPath.LAYOUT_OCR


def _field(value, confidence=0.97):
    return ExtractedValue(value=value, confidence=confidence, source=SOURCE)


def make_fields(legal_name, tin, **overrides) -> ExtractedFields:
    defaults = dict(
        legal_name=_field(legal_name),
        dba_name=_field(None),
        tax_classification=TaxClassification(value="c_corporation", confidence=0.97, source=SOURCE),
        address=Address(street=_field("1200 Industrial Pkwy"), city=_field("Columbus"), state=_field("OH"), zip=_field("43215")),
        tin=TinInfo(type="EIN", value_masked="xx-xxx1093", value_token=tokenize_tin(tin) if tin else None, format_valid=bool(tin), confidence=0.97, source=SOURCE),
        certification=Certification(signed=True, signature_present=True, date="09/15/2026", confidence=0.97),
    )
    defaults.update(overrides)
    return ExtractedFields(**defaults)


def make_supplier_df(**overrides) -> pd.DataFrame:
    row = dict(
        supplier_id="sup_00417",
        legal_name="Acme Corporation",
        dba_name="",
        tin_token=tokenize_tin("27-4821093"),
        tax_classification="c_corporation",
        address_street="1150 Industrial Pkwy",
        address_city="Columbus",
        address_state="OH",
        address_zip="43215",
        bank_routing_hash=hash_identifier("021000021"),
        bank_account_last4="4821",
        bank_account_holder_name="Acme Corporation",
        status="active",
        last_updated_at="2025-11-02",
    )
    row.update(overrides)
    return pd.DataFrame([row])


def test_normalize_business_name_strips_suffixes_and_punctuation():
    assert normalize_business_name("Acme Corporation") == normalize_business_name("ACME CORP")
    assert normalize_business_name("Acme, Corp.") == normalize_business_name("Acme Corporation")


def test_tin_match_with_all_caps_name_is_not_a_false_mismatch():
    # Regression test: rapidfuzz does not lowercase by default. Without
    # normalizing case before scoring, "ACME CORPORATION INC" against
    # "Acme Corporation" on file scored ~0.24 instead of ~0.89 and would
    # have incorrectly escalated a clean match as a possible TIN hijack.
    fields = make_fields("ACME CORPORATION INC", "27-4821093")
    result = match_supplier(fields, make_supplier_df())
    assert result.action == DecisionAction.UPDATE_EXISTING
    assert result.matched_supplier_id == "sup_00417"
    assert result.match_evidence.name_similarity_score >= 0.70


def test_exact_tin_and_name_match_updates_existing():
    fields = make_fields("Acme Corporation", "27-4821093")
    result = match_supplier(fields, make_supplier_df())
    assert result.action == DecisionAction.UPDATE_EXISTING
    assert result.matched_supplier_id == "sup_00417"
    assert result.match_evidence.tin_match is True


def test_tin_match_with_name_mismatch_escalates():
    fields = make_fields("Shell Ventures Group", "27-4821093")
    result = match_supplier(fields, make_supplier_df())
    assert result.action == DecisionAction.ROUTE_TO_HUMAN_REVIEW
    assert result.matched_supplier_id is None
    assert any(r.code == "WARN_NAME_MISMATCH_ON_TIN_MATCH" for r in result.decision_reasoning)


def test_no_tin_or_name_match_creates_new():
    fields = make_fields("Totally Unrelated Widgets Co", "99-9999999",
                          address=Address(street=_field("1 Nowhere Rd"), city=_field("Nowhere"), state=_field("WY"), zip=_field("82001")))
    result = match_supplier(fields, make_supplier_df())
    assert result.action == DecisionAction.CREATE_NEW
    assert result.matched_supplier_id is None


def test_banking_change_overrides_clean_match():
    fields = make_fields("Acme Corporation", "27-4821093")
    payload = SecondaryPayload(banking=BankingInfo(
        routing_number="071000013", account_number_last4="9999", account_holder_name="Acme Corporation",
    ))
    result = match_supplier(fields, make_supplier_df(), secondary_payload=payload)
    assert result.action == DecisionAction.ROUTE_TO_HUMAN_REVIEW
    assert result.match_evidence.banking_change_detected is True
    assert any(r.code == "WARN_HIGH_RISK_BANKING_CHANGE" for r in result.decision_reasoning)


def test_matching_banking_on_file_does_not_escalate():
    fields = make_fields("Acme Corporation", "27-4821093")
    payload = SecondaryPayload(banking=BankingInfo(
        routing_number="021000021", account_number_last4="4821", account_holder_name="Acme Corporation",
    ))
    result = match_supplier(fields, make_supplier_df(), secondary_payload=payload)
    assert result.action == DecisionAction.UPDATE_EXISTING
    assert result.match_evidence.banking_change_detected is False


def _two_supplier_df() -> pd.DataFrame:
    acme = make_supplier_df()
    globex = make_supplier_df(
        supplier_id="sup_00892", legal_name="Globex Industries Inc", dba_name="Globex",
        tin_token=tokenize_tin("45-1122334"), address_street="500 Commerce Dr",
        address_city="Austin", address_state="TX", address_zip="73301",
        bank_routing_hash=hash_identifier("111000025"), bank_account_last4="7788",
        bank_account_holder_name="Globex Industries Inc",
    )
    return pd.concat([acme, globex], ignore_index=True)


def test_shared_bank_account_across_different_suppliers_is_flagged():
    payload = SecondaryPayload(banking=BankingInfo(
        routing_number="111000025", account_number_last4="7788", account_holder_name="Some New Vendor LLC",
    ))
    result = check_shared_bank_account(payload, _two_supplier_df(), exclude_supplier_id=None)
    assert result.shared is True
    assert result.conflicting_supplier_id == "sup_00892"


def test_reusing_own_bank_account_is_not_flagged():
    # Same account as sup_00417 (Acme) itself -- normal, not a signal, when excluded as "self".
    payload = SecondaryPayload(banking=BankingInfo(
        routing_number="021000021", account_number_last4="4821", account_holder_name="Acme Corporation",
    ))
    result = check_shared_bank_account(payload, _two_supplier_df(), exclude_supplier_id="sup_00417")
    assert result.shared is False


def test_no_secondary_payload_is_not_flagged():
    result = check_shared_bank_account(None, _two_supplier_df(), exclude_supplier_id=None)
    assert result.shared is False
