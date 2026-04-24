"""Service layer for court case data retrieval."""

from typing import Optional
from sqlalchemy.orm import Session
from ngm.database.models import CourtCase, CourtCaseHearing, CaseEntity


class CourtCaseService:
    """Service for retrieving court case data."""

    def __init__(self, session: Session):
        self.session = session

    def get_case_detail(
        self, court_identifier: str, case_number: str
    ) -> Optional[CourtCase]:
        """
        Retrieve complete case details including hearings and entities.

        Args:
            court_identifier: Court identifier (e.g., 'supreme', 'kathmandudc')
            case_number: Case number (e.g., '081-CR-0081')

        Returns:
            CourtCase object with hearings and entities loaded, or None if not found
        """
        case = (
            self.session.query(CourtCase)
            .filter(
                CourtCase.court_identifier == court_identifier,
                CourtCase.case_number == case_number,
            )
            .first()
        )

        if not case:
            return None

        # Load hearings
        hearings = (
            self.session.query(CourtCaseHearing)
            .filter(
                CourtCaseHearing.court_identifier == court_identifier,
                CourtCaseHearing.case_number == case_number,
            )
            .order_by(CourtCaseHearing.hearing_date_ad.desc())
            .all()
        )

        # Load entities
        entities = (
            self.session.query(CaseEntity)
            .filter(
                CaseEntity.court_identifier == court_identifier,
                CaseEntity.case_number == case_number,
            )
            .all()
        )

        # Attach to case object for easy access
        case.hearings = hearings
        case.entities = entities

        # Expunge from session to avoid detached instance issues
        self.session.expunge(case)
        for hearing in hearings:
            self.session.expunge(hearing)
        for entity in entities:
            self.session.expunge(entity)

        return case
