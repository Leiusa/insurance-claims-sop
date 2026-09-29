from datetime import date

from sop.insurance.facts import amounts_in_sheet, build_fact_sheet, map_document
from sop.insurance.resolver import CaseHints, match_claims


def ids(claims):
    return [c.case_id for c in claims]


def test_january_healthcare_is_ambiguous(repo):
    matches = match_claims(repo.claims_for("P9"), CaseHints(case_type="healthcare", month=1))
    assert sorted(ids(matches)) == ["CL-2011", "CL-2048"]


def test_denied_january_healthcare_is_unique(repo):
    hints = CaseHints(case_type="healthcare", status="denied", month=1)
    assert ids(match_claims(repo.claims_for("P9"), hints)) == ["CL-2048"]


def test_synonyms(repo):
    hints = CaseHints(case_type="medical", status="rejected")
    assert ids(match_claims(repo.claims_for("P9"), hints)) == ["CL-2048"]


def test_year_disambiguates(repo):
    hints = CaseHints(case_type="healthcare", month=1, year=2025)
    assert ids(match_claims(repo.claims_for("P9"), hints)) == ["CL-2011"]


def test_other_customers_case_is_invisible(repo):
    assert match_claims(repo.claims_for("P7"), CaseHints(case_id="CL-2048")) == []


def test_document_mapping(repo):
    keys = list(repo.guideline["document_guidance"])
    assert map_document("pathology report", keys) == "original pathology report"
    assert map_document("office note", keys) == "treating provider office note"
    assert map_document("diagnosis report", keys) is None


def test_fact_sheet_past_deadline(repo):
    sheet = build_fact_sheet(repo.claim("CL-2048"), repo, date(2026, 9, 28))
    assert sheet["derived"]["appeal_deadline_passed"] is True
    assert "passed" in sheet["timing_note"]
    texts = [f["guidance"] for f in sheet["guidance"]["followup_topics"]]
    assert texts and all("{" not in t for t in texts)
    assert all("CL-2048" in t for t in texts)
    assert not any("within a week" in t for t in texts)  # conflicting generic guidance removed
    assert amounts_in_sheet(sheet) == {"0.00", "1450.00"}


def test_fact_sheet_before_deadline(repo):
    sheet = build_fact_sheet(repo.claim("CL-2048"), repo, date(2026, 3, 1))
    assert sheet["derived"]["appeal_deadline_passed"] is False
    assert sheet["derived"]["days_until_deadline"] == 17
    assert any(f["topic"] == "submission_timing" for f in sheet["guidance"]["followup_topics"])


def test_unmapped_document_falls_back_to_default_alternatives(repo):
    sheet = build_fact_sheet(repo.claim("CL-3001"), repo, date(2026, 9, 28))
    doc = sheet["guidance"]["documents"][0]
    assert doc["matched_guideline_entry"] is None
    assert doc["if_not_available"] == repo.guideline["document_alternative_guidance"]["default"]["en"]


def test_open_claim_has_no_document_followups(repo):
    sheet = build_fact_sheet(repo.claim("CL-2102"), repo, date(2026, 9, 28))
    assert "followup_topics" not in sheet["guidance"]
    assert sheet["derived"]["payment_issued"] is False
