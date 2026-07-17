"""Unit tests for the backfill transform (ngm.scripts.backfill_case_status).

Exercises the pure ``compute_case_updates`` — no DB. Issue IDs (DQ-01…03) refer
to the court-case data-quality baseline.
"""

from ngm.scripts.backfill_case_status import compute_case_updates


def test_header_artifact_row_is_cleared():
    # DQ-01: the ~103k Supreme rows whose status is the column header.
    updates, meta = compute_case_updates("आदेश /फैसलाको किसिम", None, None, None, None)
    assert updates == {"case_status": None}
    assert meta["lifecycle_status"] == "UNKNOWN"


def test_arrow_row_sets_verdict_type_only():
    # DQ-02: outcome enum from status; arrow form carries no date.
    updates, meta = compute_case_updates(
        "फैसला / अन्तिम आदेश >> अभियोग दावी पुग्ने", None, None, None, None
    )
    assert updates == {"verdict_type": "CONVICTED"}
    assert meta["verdict_type"] == "CONVICTED"


def test_paren_row_fills_verdict_date():
    # DQ-03: Special-court shape — recover the verdict date from case_status.
    updates, meta = compute_case_updates("फैसला (मिती: २०८२/०९/२८)", None, None, None, None)
    assert updates["verdict_date_bs"] == "2082-09-28"
    assert updates["verdict_date_ad"] is not None
    assert meta["lifecycle_status"] == "DECIDED"


def test_paren_row_uses_hearing_fallback_for_verdict_type():
    hearings = [{"case_status": "फैसला", "decision_type": "सफाई"}]
    updates, meta = compute_case_updates("फैसला (मिती: २०८२/०९/२८)", None, None, None, hearings)
    assert updates["verdict_type"] == "ACQUITTED"
    assert updates["verdict_date_bs"] == "2082-09-28"


def test_idempotent_when_already_correct():
    # Re-running must be a no-op: verdict_type already set, arrow has no date.
    updates, _ = compute_case_updates(
        "फैसला / अन्तिम आदेश >> डिसमिस", "DISMISSED", None, None, None
    )
    assert updates == {}


def test_existing_verdict_date_not_overwritten():
    updates, _ = compute_case_updates(
        "फैसला (मिती: २०८२/०९/२८)", "CONVICTED", "2081-01-17", "already", None
    )
    assert "verdict_date_bs" not in updates
    assert updates == {}


def test_pending_row_no_column_updates():
    updates, meta = compute_case_updates("चालु", None, None, None, None)
    assert updates == {}
    assert meta["lifecycle_status"] == "PENDING"
