"""
Tests for the Reader. (More are added in later steps: the labeled-document accuracy test,
quote checks, the cache test over whole files, etc.)  No network or API key needed.
"""
import json
from datetime import date

import pytest
from pydantic import ValidationError

from collectors.common import files
from reader.schema import (ExtractionOutput, FilingRecord, fields_missing_quotes, parse_amount,
                           parse_date, parse_llm_json)
from scout_reader_schema import TABLES, upsert
from tests.b_fixtures import bcon  # noqa: F401


def test_amounts_dates_and_states_are_cleaned():
    out = ExtractionOutput.model_validate({
        "filing_type": "Mechanics Lien", "lien_amount": "$1,240,000.00", "loan_amount": 38500000,
        "recording_date": "March 3, 2026", "maturity_date": "04/15/2027", "filing_date": "2026-01-02",
        "property_state": "Michigan", "property_zip": "ZIP 48104-1234", "property_type": "Student Housing",
        "loan_kind": None, "page_still_unopened": "true", "borrower_name": "  null ",
        "distress_tags": ["Contractor Unpaid", "permit-extended"], "confidence": "HIGH",
    })
    assert out.filing_type.value == "mechanics_lien"
    assert out.lien_amount == 1240000 and out.loan_amount == 38500000
    assert out.recording_date == date(2026, 3, 3) and out.maturity_date == date(2027, 4, 15)
    assert out.property_state == "MI" and out.property_zip == "48104-1234"
    assert out.property_type.value == "student_housing" and out.loan_kind is None
    assert out.page_still_unopened is True and out.borrower_name is None
    assert [t.value for t in out.distress_tags] == ["contractor_unpaid", "permit_extended"]
    assert out.confidence.value == "high"


def test_missing_optional_things_get_safe_defaults():
    out = ExtractionOutput.model_validate({"filing_type": "other"})
    assert out.property_type.value == "unknown" and out.confidence.value == "low"
    assert out.other_parties == [] and out.distress_tags == [] and out.quotes == {}


@pytest.mark.parametrize("bad", [
    {"filing_type": "banana"},                                        # not an allowed filing type
    {"filing_type": "other", "distress_tags": ["made_up_tag"]},       # not an allowed tag
    {"filing_type": "other", "recording_date": "next Tuesday"},       # not a date
    {"filing_type": "other", "loan_amount": "$100 to $200"},          # not one plain number
    {"filing_type": "other", "property_state": "Atlantis"},           # not a state
    {"borrower_name": "X LLC"},                                       # filing_type is required
])
def test_bad_ai_output_is_rejected_with_a_readable_error(bad):
    with pytest.raises(ValidationError):
        ExtractionOutput.model_validate(bad)


def test_parse_helpers():
    assert parse_amount("$1,250,000.00") == 1250000 and parse_amount("N/A") is None
    assert parse_date("2026-11-07") == date(2026, 11, 7) and parse_date("") is None


def test_parse_llm_json_handles_fences_and_chatter():
    assert parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_llm_json('Here you go: {"a": {"b": 2}} Hope that helps') == {"a": {"b": 2}}
    for junk in ("no json here", '{"a": ', "[1, 2]"):
        with pytest.raises(ValueError):
            parse_llm_json(junk)


def test_fields_missing_quotes():
    out = ExtractionOutput.model_validate({
        "filing_type": "mechanics_lien", "lien_claimant": "Acme HVAC", "lien_amount": 5,
        "quotes": {"lien_claimant": "Acme HVAC"}})
    assert fields_missing_quotes(out) == ["lien_amount"]


def test_filing_record_row_fits_the_filing_fields_table(bcon):
    out = ExtractionOutput.model_validate({
        "filing_type": "mechanics_lien", "lien_claimant": "Acme HVAC", "lien_amount": "$5,000",
        "other_parties": [{"name": "Bank", "role": "lender"}], "distress_tags": ["contractor_unpaid"],
        "quotes": {"lien_claimant": "Acme HVAC"}, "confidence": "high"})
    rec = FilingRecord(**out.model_dump(), file_id="f1", model_name="m", prompt_version="v2",
                       input_tokens=10, output_tokens=5, est_cost_usd=0.01)
    upsert(bcon, "filing_fields", rec.to_db_row())
    row = bcon.execute("SELECT filing_type, lien_amount, status, other_parties::TEXT, distress_tags::TEXT, "
                       "quotes::TEXT FROM filing_fields WHERE file_id='f1'").fetchone()
    assert row[0] == "mechanics_lien" and row[1] == 5000 and row[2] == "ok"
    assert json.loads(row[3]) == [{"name": "Bank", "role": "lender"}]
    assert json.loads(row[4]) == ["contractor_unpaid"] and json.loads(row[5]) == {"lien_claimant": "Acme HVAC"}


def test_filing_fields_has_every_column_in_the_spec(bcon):
    cols = {r[1] for r in bcon.execute("PRAGMA table_info('filing_fields')").fetchall()}
    expected = set(ExtractionOutput.model_fields) - {"quotes_placeholder"}
    assert expected <= cols
    assert {"file_id", "status", "review_reason", "model_name", "prompt_version", "extracted_at",
            "input_tokens", "output_tokens", "est_cost_usd"} <= cols


def test_all_part_b_tables_exist(bcon):
    have = {r[0] for r in bcon.execute("SHOW TABLES").fetchall()}
    assert set(TABLES) <= have


def test_scout_sources_are_accepted_by_part_as_raw_files(bcon):
    for src, cat in [("project_site", "project_website"), ("news", "news_article"),
                     ("auction", "auction_listing"), ("wsj_notice", "ucc_sale_notice")]:
        r = files.store_file(bcon, f"{src} page".encode(), source=src, doc_category=cat,
                             collector_name="t", state="MI", ext="html")
        assert r.status == "new"


def test_loan_kind_and_address_parts_do_not_need_their_own_quote():
    """Real Gemini output: loan_kind is a category, and city/state/zip come out of the quoted address line."""
    out = ExtractionOutput.model_validate({
        "filing_type": "recorded_loan", "loan_kind": "permanent", "property_address": "100 Sample St, Ann Arbor, MI 48104",
        "property_city": "Ann Arbor", "property_state": "MI", "property_zip": "48104",
        "quotes": {"property_address": "100 Sample St, Ann Arbor, MI 48104"}})
    assert fields_missing_quotes(out) == []
    # ...but a city that the address quote does NOT contain still needs its own quote
    bad = ExtractionOutput.model_validate({
        "filing_type": "recorded_loan", "property_address": "100 Sample St", "property_city": "Detroit",
        "quotes": {"property_address": "100 Sample St"}})
    assert fields_missing_quotes(bad) == ["property_city"]
