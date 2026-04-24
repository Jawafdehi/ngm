"""API routes for court case data."""

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from ngm.database.models import get_engine, get_session
from ngm.api.service import CourtCaseService
from ngm.api.models import CourtCaseDetailResponse, HearingResponse, CaseEntityResponse

router = APIRouter(prefix="/api/ngm", tags=["court_cases"])


def get_db():
    """Dependency for database session."""
    engine = get_engine()
    session = get_session(engine)
    try:
        yield session
    finally:
        session.close()


@router.get(
    "/court_case/{case_id}",
    response_model=CourtCaseDetailResponse,
    summary="Get court case details",
    description="Retrieve complete case details including hearings and entities. "
    "Case ID format: {court}:{case_number} (e.g., supreme:081-CR-0081)",
)
def get_court_case_detail(case_id: str, db: Session = Depends(get_db)):
    """
    Get detailed information about a specific court case.

    Args:
        case_id: Composite ID in format {court}:{case_number}
                 Example: supreme:081-CR-0081

    Returns:
        Complete case details with hearings and entities

    Raises:
        HTTPException: 400 if case_id format is invalid
        HTTPException: 404 if case not found
    """
    # Parse case_id
    parts = case_id.split(":", 1)
    if len(parts) != 2:
        raise HTTPException(
            status_code=400,
            detail="Invalid case_id format. Expected format: {court}:{case_number} (e.g., supreme:081-CR-0081)",
        )

    court_identifier, case_number = parts

    # Retrieve case
    service = CourtCaseService(db)
    case = service.get_case_detail(court_identifier, case_number)

    if not case:
        raise HTTPException(
            status_code=404,
            detail=f"Case not found: {case_id}",
        )

    # Convert to response model
    hearings = [HearingResponse.model_validate(h) for h in case.hearings]
    entities = [CaseEntityResponse.model_validate(e) for e in case.entities]

    response = CourtCaseDetailResponse.model_validate(case)
    response.hearings = hearings
    response.entities = entities

    return response
