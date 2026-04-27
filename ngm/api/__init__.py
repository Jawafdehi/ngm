"""NGM Court Case Query Library.

This module provides a simple interface for querying court case data
from the NGM database without requiring a web server.
"""

from ngm.api.service import CourtCaseService

__all__ = [
    "CourtCaseService",
]
