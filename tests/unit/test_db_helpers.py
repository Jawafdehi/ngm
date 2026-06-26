"""Unit tests for ngm.utils.db_helpers — real BS dates (values verified)."""

from datetime import date

from ngm.database.models import CourtCase
from ngm.utils.db_helpers import CaseCache, convert_bs_to_ad


def test_convert_bs_to_ad_real():
    # Verified with the `nepali` lib: 2081-09-28 BS == 2025-01-12 AD.
    assert convert_bs_to_ad("2081-09-28") == date(2025, 1, 12)
    assert convert_bs_to_ad("2078-05-08") == date(2021, 8, 24)


def test_convert_bs_to_ad_invalid_returns_none():
    # Must never raise on the corrupted sentinel / garbage / wrong arity.
    assert convert_bs_to_ad("****-**-**") is None
    assert convert_bs_to_ad("") is None
    assert convert_bs_to_ad("2081-09") is None


def test_case_cache_keys_by_number_and_court():
    cache = CaseCache()
    case = CourtCase(case_number="082-CR-0003", court_identifier="special")
    cache.set(case)
    assert cache.get("082-CR-0003", "special") is case
    # Same case number, different court -> distinct key, cache miss.
    assert cache.get("082-CR-0003", "supreme") is None
