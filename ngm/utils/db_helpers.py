"""Database helper functions for court case scrapers."""

from datetime import date
from typing import Dict, Tuple
from nepali.datetime import nepalidate
from sqlalchemy.orm import Session
from ngm.database.models import CourtCase, CourtScrapedDate
import logging


def convert_bs_to_ad(date_bs: str) -> date | None:
    """Convert BS date string to AD date object."""
    if not date_bs:
        return None
    try:
        parts = date_bs.split("-")
        if len(parts) != 3:
            return None
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        nepali_date = nepalidate(year, month, day)
        return nepali_date.to_datetime().date()
    except Exception as e:
        # Log the error but don't fail silently
        logging.error(f"Failed to convert BS date {date_bs}: {e}")
        return None


def get_scraped_dates(session: Session, court_id: str) -> set[str]:
    """Get all scraped dates (BS format) for a court."""
    with session.begin():
        results = (
            session.query(CourtScrapedDate.date_bs)
            .filter_by(court_identifier=court_id)
            .all()
        )
        return {row[0] for row in results}


def mark_date_scraped(session: Session, court_id: str, date_bs: str, data: str = None):
    """Mark a date (BS format) as scraped for a court."""
    scraped = CourtScrapedDate(court_identifier=court_id, date_bs=date_bs, data=data)
    session.add(scraped)


class CaseCache:
    """Cache for CourtCase objects to avoid repeated DB queries."""

    def __init__(self):
        self._cache: Dict[Tuple[str, str], CourtCase] = {}

    def get(self, case_number: str, court_id: str) -> CourtCase | None:
        return self._cache.get((case_number, court_id))

    def set(self, case: CourtCase):
        self._cache[(case.case_number, case.court_identifier)] = case

    def clear(self):
        self._cache.clear()
