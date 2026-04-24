"""Case number normalization utilities."""

import re


def normalize_case_number(case_number: str) -> str:
    """
    Normalize case number to standard format.

    Handles:
    - Uppercase/lowercase: 081-cr-0081 → 081-CR-0081
    - Missing leading zeros: 81-cr-0081 → 081-CR-0081
    - Nepali numerals: ०८१-CR-००८१ → 081-CR-0081

    Args:
        case_number: Case number in any supported format

    Returns:
        Normalized case number in format: DDD-SS-DDDD (e.g., 081-CR-0081)

    Examples:
        >>> normalize_case_number("081-cr-0081")
        '081-CR-0081'
        >>> normalize_case_number("81-cr-81")
        '081-CR-0081'
        >>> normalize_case_number("०८१-CR-००८१")
        '081-CR-0081'
    """
    # Nepali to English digit mapping
    nepali_to_english = {
        "०": "0",
        "१": "1",
        "२": "2",
        "३": "3",
        "४": "4",
        "५": "5",
        "६": "6",
        "७": "7",
        "८": "8",
        "९": "9",
    }

    # Convert Nepali numerals to English
    normalized = case_number
    for nepali, english in nepali_to_english.items():
        normalized = normalized.replace(nepali, english)

    # Split by dash
    parts = normalized.split("-")
    if len(parts) != 3:
        # Return as-is if format is unexpected
        return normalized.upper()

    # Normalize each part
    fiscal_year = parts[0].zfill(3)  # Pad to 3 digits
    case_type = parts[1].upper()  # Uppercase
    case_seq = parts[2].zfill(4)  # Pad to 4 digits

    return f"{fiscal_year}-{case_type}-{case_seq}"
