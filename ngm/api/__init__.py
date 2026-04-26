"""NGM Court Case Query Library.

This module provides a simple interface for querying court case data
from the NGM database without requiring a web server.
"""

from ngm.api.service import CourtCaseService
from ngm.api.models import (
    CourtCaseDetailResponse,
    HearingResponse,
    CaseEntityResponse,
)

__all__ = [
    "CourtCaseService",
    "CourtCaseDetailResponse",
    "HearingResponse",
    "CaseEntityResponse",
]
