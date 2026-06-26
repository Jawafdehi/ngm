"""Unit tests for spider-level pure parse helpers (real example inputs)."""

from bs4 import BeautifulSoup

from ngm.ngscrape.spiders.high_court_cases import HighCourtCasesSpider
from ngm.ngscrape.spiders.supreme_case_enrichment import (
    _map_field,
    _split_parties,
    extract_caseno,
)
from ngm.ngscrape.spiders.supreme_court_cases import SupremeCourtCasesSpider


def test_split_parties_samet_and_slash():
    # 'समेत' (et al.) stripped; parties split on slash.
    assert _split_parties("रामबहादुर/श्यामबहादुर समेत") == ["रामबहादुर", "श्यामबहादुर"]


def test_split_parties_comma_within_slash():
    assert _split_parties("क, ख / ग") == ["क", "ख", "ग"]


def test_map_field_registration_date_normalizes():
    data = {}
    _map_field(data, "दर्ता मिती", "२०८१/०९/२८")
    assert data["registration_date_bs"] == "2081-09-28"
    assert data["registration_date_ad"] is not None  # BS->AD conversion succeeded


def test_map_field_verdict_sentinel_does_not_fabricate_ad():
    # Current (correct) behavior: AD is intentionally NOT set for the sentinel.
    data = {}
    _map_field(data, "फैसला मिती", "**** ** **")
    assert "verdict_date_ad" not in data


def test_map_field_verdict_sentinel_bs_not_stored():
    # I6 fix: the "no verdict yet" sentinel must not be persisted as a fake date.
    data = {}
    _map_field(data, "फैसला मिती", "**** ** **")
    assert "verdict_date_bs" not in data
    assert "verdict_date_ad" not in data


def test_supreme_clean_division_strips_dash_underscore():
    sp = SupremeCourtCasesSpider.__new__(SupremeCourtCasesSpider)
    assert sp._clean_division("- रिट १ _") == "रिट १"


def test_high_clean_case_number_drops_parenthetical():
    sp = HighCourtCasesSpider.__new__(HighCourtCasesSpider)
    cell = BeautifulSoup("<td>082-CR-0048<br/>(पुनरावेदन)</td>", "html.parser").td
    assert sp._clean_case_number(cell) == "082-CR-0048"


def test_extract_caseno_basic():
    assert extract_caseno("sys.php?d=reports&mode=view&caseno=12345") == "12345"


def test_extract_caseno_preserves_equals():
    # I14 fix: a value containing '=' is no longer truncated at the first '='.
    assert extract_caseno("sys.php?mode=view&caseno=AB==CD") == "AB==CD"


def test_extract_caseno_missing():
    assert extract_caseno("sys.php?mode=view") is None
