"""Service layer for court case data retrieval."""

from typing import Optional
from sqlalchemy.orm import Session, sessionmaker, joinedload
from ngm.database import get_engine
from ngm.database.models import CourtCase, CourtCaseHearing, CaseEntity
from ngm.utils.case_normalizer import normalize_case_number


class CourtCaseService:
    """Service for retrieving court case data.

    Example usage:
        from ngm.api import CourtCaseService

        # Create service instance
        service = CourtCaseService()

        # Query a case
        case = service.get_case_detail("supreme", "081-CR-0081")

        if case:
            print(f"Case: {case.case_number}")
            print(f"Plaintiff: {case.plaintiff}")
            print(f"Defendant: {case.defendant}")
            print(f"Hearings: {len(case.hearings)}")
            print(f"Entities: {len(case.entities)}")

        # Close when done
        service.close()
    """

    def __init__(self, session: Optional[Session] = None):
        """Initialize the service.

        Args:
            session: Optional SQLAlchemy session. If not provided, creates a new one.
        """
        if session:
            self.session = session
            self._owns_session = False
        else:
            engine = get_engine()
            SessionLocal = sessionmaker(bind=engine, autoflush=False)
            self.session = SessionLocal()
            self._owns_session = True

    def close(self):
        """Close the database session if owned by this service."""
        if self._owns_session and self.session:
            self.session.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()

    def get_case_detail(
        self, court_identifier: str, case_number: str, normalize: bool = True
    ) -> Optional[CourtCase]:
        """
        Retrieve complete case details including hearings and entities.

        Args:
            court_identifier: Court identifier (e.g., 'supreme', 'kathmandudc')
            case_number: Case number (e.g., '081-CR-0081')
            normalize: Whether to normalize the case number (default: True)

        Returns:
            CourtCase object with hearings and entities loaded, or None if not found.
            Note: The returned objects are detached from the session. Accessing
            lazy-loaded relationships (like case.court) after the service is closed
            will raise DetachedInstanceError.
        """
        # Normalize case number if requested
        if normalize:
            case_number = normalize_case_number(case_number)

        # Eager load the court relationship to avoid DetachedInstanceError
        case = (
            self.session.query(CourtCase)
            .options(joinedload(CourtCase.court))
            .filter(
                CourtCase.court_identifier == court_identifier,
                CourtCase.case_number == case_number,
            )
            .first()
        )

        if not case:
            return None

        # Load hearings with court relationship
        hearings = (
            self.session.query(CourtCaseHearing)
            .options(joinedload(CourtCaseHearing.court))
            .filter(
                CourtCaseHearing.court_identifier == court_identifier,
                CourtCaseHearing.case_number == case_number,
            )
            .order_by(CourtCaseHearing.hearing_date_ad.desc())
            .all()
        )

        # Load entities with court relationship
        entities = (
            self.session.query(CaseEntity)
            .options(joinedload(CaseEntity.court))
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
        # This is necessary when the service owns the session and will close it
        self.session.expunge(case)
        for hearing in hearings:
            self.session.expunge(hearing)
        for entity in entities:
            self.session.expunge(entity)

        return case
