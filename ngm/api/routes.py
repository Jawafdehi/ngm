"""API routes for court case data."""

from fastapi import APIRouter, HTTPException, Depends, Request
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address
from ngm.database.models import get_engine, get_session
from ngm.api.service import CourtCaseService
from ngm.api.models import CourtCaseDetailResponse, HearingResponse, CaseEntityResponse
from ngm.utils.case_normalizer import normalize_case_number

# Rate limiter based on IP address
limiter = Limiter(key_func=get_remote_address)

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
    "Case ID format: {court}:{case_number} (e.g., supreme:081-CR-0081). "
    "Case numbers are normalized automatically (accepts 081-cr-0081, 81-cr-81, ०८१-CR-००८१).",
)
@limiter.limit("30/minute")  # 30 requests per minute per IP
def get_court_case_detail(
    request: Request, case_id: str, db: Session = Depends(get_db)
):
    """
    Get detailed information about a specific court case.

    Args:
        request: FastAPI request object (for rate limiting)
        case_id: Composite ID in format {court}:{case_number}
                 Example: supreme:081-CR-0081
                 Also accepts: supreme:081-cr-0081, supreme:81-cr-81, supreme:०८१-CR-००८१

    Returns:
        Complete case details with hearings and entities

    Raises:
        HTTPException: 400 if case_id format is invalid
        HTTPException: 404 if case not found
        HTTPException: 429 if rate limit exceeded
    """
    # Parse case_id
    parts = case_id.split(":", 1)
    if len(parts) != 2:
        raise HTTPException(
            status_code=400,
            detail="Invalid case_id format. Expected format: {court}:{case_number} (e.g., supreme:081-CR-0081)",
        )

    court_identifier, case_number = parts

    # Normalize case number (handle different formats)
    case_number = normalize_case_number(case_number)

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
