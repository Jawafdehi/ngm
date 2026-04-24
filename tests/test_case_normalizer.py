"""Tests for case number normalization."""

from ngm.utils.case_normalizer import normalize_case_number


def test_normalize_uppercase():
    """Test normalization of lowercase case numbers."""
    assert normalize_case_number("081-cr-0081") == "081-CR-0081"
    assert normalize_case_number("082-oa-0503") == "082-OA-0503"


def test_normalize_missing_leading_zeros():
    """Test normalization of case numbers without leading zeros."""
    assert normalize_case_number("81-cr-81") == "081-CR-0081"
    assert normalize_case_number("82-oa-503") == "082-OA-0503"
    assert normalize_case_number("1-cr-1") == "001-CR-0001"


def test_normalize_nepali_numerals():
    """Test normalization of Nepali numerals to English."""
    assert normalize_case_number("०८१-CR-००८१") == "081-CR-0081"
    assert normalize_case_number("०८२-OA-०५०३") == "082-OA-0503"
    assert normalize_case_number("०७९-cr-०१२०") == "079-CR-0120"


def test_normalize_mixed_formats():
    """Test normalization of mixed format case numbers."""
    assert normalize_case_number("81-cr-0081") == "081-CR-0081"
    assert normalize_case_number("081-cr-81") == "081-CR-0081"
    assert normalize_case_number("०८१-cr-0081") == "081-CR-0081"


def test_normalize_already_normalized():
    """Test that already normalized case numbers remain unchanged."""
    assert normalize_case_number("081-CR-0081") == "081-CR-0081"
    assert normalize_case_number("082-OA-0503") == "082-OA-0503"


def test_normalize_invalid_format():
    """Test normalization with invalid format (returns uppercase)."""
    # Missing dashes
    assert normalize_case_number("081CR0081") == "081CR0081"
    # Too many parts
    assert normalize_case_number("081-CR-0081-X") == "081-CR-0081-X"
    # Too few parts
    assert normalize_case_number("081-CR") == "081-CR"


def test_normalize_all_nepali_digits():
    """Test all Nepali digits are converted correctly."""
    assert normalize_case_number("०१२३४५६७८९-CR-०१२३") == "0123456789-CR-0123"
