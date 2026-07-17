"""Unit tests for ngm.utils.normalizer — real Nepali example values."""

import pytest

from ngm.utils.normalizer import (
    coerce_count,
    fix_parenthesis_spacing,
    nepali_to_roman_numerals,
    normalize_date,
    normalize_whitespace,
    roman_to_nepali_numerals,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("२०८१/०९/२८", "2081-09-28"),  # Devanagari digits + slash (district reg date)
        ("२०७८।०५।०८", "2078-05-08"),  # Devanagari + danda U+0964 (verdict date)
        ("2082.4.16", "2082-04-16"),  # roman + period, needs zero-padding
        ("2081/9/28", "2081-09-28"),
        ("", ""),
    ],
)
def test_normalize_date_real(raw, expected):
    assert normalize_date(raw) == expected


def test_normalize_date_sentinel_current_behavior():
    # normalize_date is a dumb separator-normalizer: the website's "no verdict
    # yet" sentinel "**** ** **" becomes "****-**-**". That string itself is
    # fine here; the BUG (I6) is that the enrichment call-site STORES it into
    # verdict_date_bs instead of leaving NULL — pinned in test_parse_helpers.
    assert normalize_date("**** ** **") == "****-**-**"


@pytest.mark.parametrize(
    "nep,rom",
    [
        ("०८२-CR-०००३", "082-CR-0003"),  # real special-court case number
        ("०८१-WO-०१२३", "081-WO-0123"),
    ],
)
def test_numeral_roundtrip(nep, rom):
    assert nepali_to_roman_numerals(nep) == rom
    assert roman_to_nepali_numerals(rom) == nep
    assert roman_to_nepali_numerals(nepali_to_roman_numerals(nep)) == nep


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("३", 3),  # Devanagari digit (as scraped from पेशी चढेको संख्या)
        ("12", 12),  # ASCII
        ("१०५", 105),  # multi-digit Devanagari
        ("पटक ७", 7),  # digit embedded in text
        ("", None),  # empty → NULL, not an error
        ("N/A", None),  # non-numeric → NULL
        (None, None),
        (5, 5),  # already an int
    ],
)
def test_coerce_count(raw, expected):
    assert coerce_count(raw) == expected


def test_fix_parenthesis_spacing():
    assert fix_parenthesis_spacing("082-CR-0048( पुनरावेदन)") == "082-CR-0048 (पुनरावेदन)"


def test_normalize_whitespace_collapses_runs():
    assert normalize_whitespace("  फाँट\tक  ") == "फाँट क"


def test_normalize_whitespace_strips_surrounding_quotes():
    assert normalize_whitespace('"भ्रष्टाचार"') == "भ्रष्टाचार"
