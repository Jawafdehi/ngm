"""Unit tests for ngm.utils.case_status_parser — real Nepali case_status values.

Values are taken verbatim from the 1.6M-row corpus audit (2026-07-16). Issue IDs
(DQ-01…05) refer to the court-case data-quality baseline.
"""

from datetime import date

import pytest

from ngm.utils.case_status_parser import (
    ACQUITTED,
    CLAIM_DENIED,
    CLAIM_UPHELD,
    CONVICTED,
    DECIDED,
    DISMISSED,
    OTHER,
    PARTIALLY_CONVICTED,
    PENDING,
    PROCEDURAL,
    SETTLED,
    UNKNOWN,
    WITHDRAWN,
    apply_status,
    parse_case_status,
    verdict_from_hearings,
)


# R1 — the 103,212-row Supreme header artifact must NOT become a status (DQ-01).
@pytest.mark.parametrize(
    "raw",
    [
        "आदेश /फैसलाको किसिम",   # exact value stored on 103,212 Supreme rows
        "आदेश/फैसलाको किसिम",    # despaced variant
        "मुद्दाको किसिम",
    ],
)
def test_r1_header_artifact_is_unknown(raw):
    parsed = parse_case_status(raw)
    assert parsed.lifecycle_status == UNKNOWN
    assert parsed.verdict_type is None


# R2 — pending in every observed spelling collapses to one enum (DQ-05).
@pytest.mark.parametrize("raw", ["चालु", "चलिरहेको", "चली रहेको", "विचाराधीन"])
def test_r2_pending_variants(raw):
    parsed = parse_case_status(raw)
    assert parsed.lifecycle_status == PENDING
    assert parsed.verdict_type is None


# R3 — arrow-form outcomes → verdict_type enum (DQ-02).
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("फैसला / अन्तिम आदेश >> दाबी पुग्ने", CLAIM_UPHELD),
        ("फैसला / अन्तिम आदेश >> माग बमोजिम हुने", CLAIM_UPHELD),
        ("फैसला / अन्तिम आदेश >> दाबी नपुग्ने", CLAIM_DENIED),
        ("फैसला / अन्तिम आदेश >> मिलापत्र", SETTLED),
        ("फैसला / अन्तिम आदेश >> डिसमिस", DISMISSED),
        ("फैसला / अन्तिम आदेश >> मुद्दा फिर्ता", WITHDRAWN),
        ("फैसला / अन्तिम आदेश >> विवाह दर्ता गरिएको", PROCEDURAL),
        # corruption-critical: charge proven / not proven (both व/ब spellings)
        ("फैसला / अन्तिम आदेश >> अभियोग दावी पुग्ने", CONVICTED),
        ("फैसला / अन्तिम आदेश >> अभियोग दाबी पुग्ने", CONVICTED),
        ("फैसला / अन्तिम आदेश >> अभियोग दाबी नपुग्ने", ACQUITTED),
        ("फैसला / अन्तिम आदेश >> आंशिक अभियोग दाबी पुग्ने", PARTIALLY_CONVICTED),
    ],
)
def test_r3_arrow_outcomes(raw, expected):
    parsed = parse_case_status(raw)
    assert parsed.lifecycle_status == DECIDED
    assert parsed.verdict_type == expected
    assert parsed.unmapped is False


def test_r3_double_space_after_arrow():
    # real value carried two spaces: "… >>  अन्य"
    parsed = parse_case_status("फैसला / अन्तिम आदेश >>  अन्य")
    assert parsed.verdict_type == OTHER
    assert parsed.verdict_outcome_raw == "अन्य"


def test_r3_unmapped_outcome_flagged_not_lost():
    parsed = parse_case_status("फैसला / अन्तिम आदेश >> कुनै नयाँ किसिम")
    assert parsed.lifecycle_status == DECIDED
    assert parsed.verdict_type == OTHER
    assert parsed.unmapped is True
    assert parsed.verdict_outcome_raw == "कुनै नयाँ किसिम"


# R4 — paren-date form recovers the verdict date the columns never got (DQ-03).
# This is the shape used by ALL 12,601 Special (corruption) cases.
@pytest.mark.parametrize(
    "raw,expected_bs",
    [
        ("फैसला (मिती: २०८२/०९/२८)", "2082-09-28"),
        ("फैसला (मिती: 2081/09/11)", "2081-09-11"),
        ("फैसला (२०८१/०१/१७)", "2081-01-17"),
    ],
)
def test_r4_paren_date_extracted(raw, expected_bs):
    parsed = parse_case_status(raw)
    assert parsed.lifecycle_status == DECIDED
    assert parsed.verdict_date_bs == expected_bs
    assert isinstance(parsed.verdict_date_ad, date)


def test_r5_bare_decided_marker():
    parsed = parse_case_status("फैसला भएको")
    assert parsed.lifecycle_status == DECIDED
    assert parsed.verdict_type is None
    assert parsed.verdict_date_bs is None


@pytest.mark.parametrize("raw", ["", None, "   "])
def test_empty_is_unknown(raw):
    assert parse_case_status(raw).lifecycle_status == UNKNOWN


# Hearing-based fallback for outcomes the status string never carried.
def test_verdict_from_hearings_acquittal():
    hearings = [
        {"case_status": "आदेश", "decision_type": "थुनछेक आदेश (धरौटी)"},
        {"case_status": "फैसला", "decision_type": "सफाई"},
    ]
    assert verdict_from_hearings(hearings) == ACQUITTED


def test_verdict_from_hearings_conviction():
    hearings = [{"case_status": "फैसला", "decision_type": "ठहर"}]
    assert verdict_from_hearings(hearings) == CONVICTED


def test_verdict_from_hearings_none_when_interlocutory_only():
    hearings = [
        {"case_status": "स्थगित", "decision_type": "प्र.को कानुन ब्यवसायी बाट"},
        {"case_status": "आदेश", "decision_type": "साक्षी बुझ्ने"},
    ]
    assert verdict_from_hearings(hearings) is None


def test_verdict_from_hearings_empty():
    assert verdict_from_hearings([]) is None
    assert verdict_from_hearings(None) is None


# apply_status — the enrichment-spider integration point.
def test_apply_status_drops_header_artifact():
    # The 103,212-row bug: header must NOT be written as case_status (DQ-01).
    target = {}
    apply_status(target, "आदेश /फैसलाको किसिम")
    assert "case_status" not in target
    assert "verdict_type" not in target


def test_apply_status_arrow_sets_status_and_verdict():
    target = {}
    apply_status(target, "फैसला / अन्तिम आदेश >> अभियोग दावी पुग्ने")
    assert target["case_status"] == "फैसला / अन्तिम आदेश >> अभियोग दावी पुग्ने"
    assert target["verdict_type"] == CONVICTED


def test_apply_status_paren_fills_verdict_date():
    target = {}
    apply_status(target, "फैसला (मिती: २०८२/०९/२८)")
    assert target["verdict_date_bs"] == "2082-09-28"
    assert target["verdict_date_ad"] is not None


def test_apply_status_does_not_overwrite_dedicated_verdict_date():
    # A dedicated "फैसला मिति" field already set the date — parser must not clobber it.
    target = {"verdict_date_bs": "2081-01-17", "verdict_date_ad": "already"}
    apply_status(target, "फैसला (मिती: २०८२/०९/२८)")
    assert target["verdict_date_bs"] == "2081-01-17"
    assert target["verdict_date_ad"] == "already"


def test_apply_status_empty_sets_nothing():
    target = {}
    apply_status(target, "")
    assert target == {}
