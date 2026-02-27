"""
Court identifier mapping utilities.

Maps between NGM database court identifiers and the Supreme Court
website's API parameters (court_type and court_id).

Database identifiers:
    - 'supreme'       → Supreme Court
    - 'special'       → Special Court
    - 'biratnagarhc'  → High Court Biratnagar
    - 'kathmandudc'   → District Court Kathmandu

Website parameters:
    - court_type: S (Supreme), T (Special), A (High), D (District)
    - court_id:   Numeric ID for high/district courts, empty for supreme/special
"""

from typing import Tuple
import logging
from ngm.utils.court_ids import HIGH_COURTS, DISTRICT_COURTS

_logger = logging.getLogger(__name__)


# Direct mapping for courts that don't need a numeric ID
COURT_TYPE_MAP = {
    "supreme": ("S", ""),
    "special": ("T", ""),
}

# Build high court mapping using explicit site_id from court_ids.py
HIGH_COURT_ID_MAP = {
    court["identifier"]: str(court["site_id"]) for court in HIGH_COURTS
}

# Reverse: numeric ID → identifier (for converting website data back to DB)
HIGH_COURT_REVERSE_MAP = {
    str(court["site_id"]): court["identifier"] for court in HIGH_COURTS
}

# Build district court mapping using explicit district_id from court_ids.py
DISTRICT_COURT_ID_MAP = {
    court["code_name"]: str(court["district_id"]) for court in DISTRICT_COURTS
}

# Reverse: district_id → code_name
DISTRICT_COURT_REVERSE_MAP = {
    str(court["district_id"]): court["code_name"] for court in DISTRICT_COURTS
}

# Validate HIGH_COURTS data integrity at load time
if len(HIGH_COURTS) == 0:
    raise ValueError("HIGH_COURTS list is empty")

if len(HIGH_COURTS) != len(HIGH_COURT_ID_MAP):
    raise ValueError(
        f"HIGH_COURTS length mismatch: expected {len(HIGH_COURTS)}, "
        f"got {len(HIGH_COURT_ID_MAP)} in mapping"
    )

# Validate site_id sequence is complete (1 to N)
expected_ids = set(range(1, len(HIGH_COURTS) + 1))
actual_ids = {court["site_id"] for court in HIGH_COURTS}
if expected_ids != actual_ids:
    missing = expected_ids - actual_ids
    extra = actual_ids - expected_ids
    raise ValueError(
        f"HIGH_COURTS site_id validation failed. "
        f"Missing IDs: {sorted(missing) if missing else 'none'}, "
        f"Extra IDs: {sorted(extra) if extra else 'none'}"
    )

_logger.debug(
    f"HIGH_COURTS mapping initialized with {len(HIGH_COURTS)} courts using explicit site_id"
)


def get_court_params(court_identifier: str) -> Tuple[str, str]:
    """
    Map NGM database court identifier to website form parameters.

    Args:
        court_identifier: Database court identifier (e.g. 'supreme', 'kathmandudc')

    Returns:
        Tuple of (court_type, court_id) for the website search form

    Raises:
        ValueError: If court_identifier is not recognised

    Examples:
        >>> get_court_params('supreme')
        ('S', '')
        >>> get_court_params('special')
        ('T', '')
        >>> get_court_params('biratnagarhc')
        ('A', '1')
        >>> get_court_params('kathmandudc')
        ('D', '39')
    """
    if court_identifier in COURT_TYPE_MAP:
        return COURT_TYPE_MAP[court_identifier]

    if court_identifier in HIGH_COURT_ID_MAP:
        return ("A", HIGH_COURT_ID_MAP[court_identifier])

    if court_identifier in DISTRICT_COURT_ID_MAP:
        return ("D", DISTRICT_COURT_ID_MAP[court_identifier])

    raise ValueError(
        f"Unknown court identifier: '{court_identifier}'. "
        f"Expected 'supreme', 'special', a high court identifier (e.g. 'biratnagarhc'), "
        f"or a district court identifier (e.g. 'kathmandudc')."
    )


def get_court_identifier(court_type: str, court_id: str) -> str:
    """
    Reverse mapping: website parameters → NGM database court identifier.

    Args:
        court_type: S / T / A / D
        court_id:   Numeric ID string, or empty for supreme/special

    Returns:
        NGM database court identifier

    Raises:
        ValueError: If the combination is not recognised

    Examples:
        >>> get_court_identifier('S', '')
        'supreme'
        >>> get_court_identifier('A', '1')
        'biratnagarhc'
        >>> get_court_identifier('D', '39')
        'kathmandudc'
    """
    if court_type == "S":
        return "supreme"

    if court_type == "T":
        return "special"

    if court_type == "A":
        if court_id in HIGH_COURT_REVERSE_MAP:
            return HIGH_COURT_REVERSE_MAP[court_id]
        raise ValueError(f"Unknown high court ID: '{court_id}'")

    if court_type == "D":
        if court_id in DISTRICT_COURT_REVERSE_MAP:
            return DISTRICT_COURT_REVERSE_MAP[court_id]
        raise ValueError(f"Unknown district court ID: '{court_id}'")

    raise ValueError(f"Unknown court type: '{court_type}'. Expected S, T, A, or D.")


def is_valid_court_identifier(court_identifier: str) -> bool:
    """
    Check whether a court identifier is valid.

    Examples:
        >>> is_valid_court_identifier('supreme')
        True
        >>> is_valid_court_identifier('notacourt')
        False
    """
    try:
        get_court_params(court_identifier)
        return True
    except ValueError:
        return False
