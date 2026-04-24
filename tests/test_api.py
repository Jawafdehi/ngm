"""Tests for the NGM Court Case API."""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import Mock, patch
from datetime import date, datetime
from ngm.api.app import app
from ngm.database.models import CourtCase, CourtCaseHearing, CaseEntity

client = TestClient(app)


@pytest.fixture
def mock_case():
    """Create a mock court case with hearings and entities."""
    case = Mock(spec=CourtCase)
    case.case_number = "081-CR-0081"
    case.court_identifier = "supreme"
    case.registration_date_bs = "2081-05-15"
    case.registration_date_ad = date(2024, 8, 30)
    case.case_type = "भ्रष्टाचार"
    case.division = None
    case.category = None
    case.section = None
    case.plaintiff = "नेपाल सरकार"
    case.defendant = "जन बहादुर गुरुङ"
    case.original_case_number = None
    case.case_id = None
    case.priority = None
    case.registration_number = None
    case.case_status = "चालु"
    case.verdict_date_bs = None
    case.verdict_date_ad = None
    case.verdict_judge = None
    case.status = "enriched"
    case.extra_data = None
    case.created_at = datetime(2024, 9, 1, 8, 0, 0)
    case.updated_at = datetime(2025, 1, 6, 10, 30, 0)

    # Mock hearing
    hearing = Mock(spec=CourtCaseHearing)
    hearing.id = 1234
    hearing.hearing_date_bs = "2081-09-20"
    hearing.hearing_date_ad = date(2025, 1, 5)
    hearing.bench = "इजलाश 31"
    hearing.bench_type = "संयुक्त इजलास"
    hearing.judge_names = "माननीय न्यायाधीश श्री कृतबहादुर वोहरा"
    hearing.lawyer_names = None
    hearing.serial_no = None
    hearing.case_status = "स्थगित"
    hearing.decision_type = None
    hearing.remarks = None
    hearing.scraped_at = datetime(2025, 1, 6, 10, 30, 0)
    hearing.extra_data = None

    # Mock entities
    plaintiff_entity = Mock(spec=CaseEntity)
    plaintiff_entity.id = 5678
    plaintiff_entity.side = "plaintiff"
    plaintiff_entity.name = "नेपाल सरकार"
    plaintiff_entity.address = "काठमाडौं"
    plaintiff_entity.nes_id = None

    defendant_entity = Mock(spec=CaseEntity)
    defendant_entity.id = 5679
    defendant_entity.side = "defendant"
    defendant_entity.name = "जन बहादुर गुरुङ"
    defendant_entity.address = "पोखरा"
    defendant_entity.nes_id = None

    case.hearings = [hearing]
    case.entities = [plaintiff_entity, defendant_entity]

    return case


def test_health_check():
    """Test the health check endpoint."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "NGM Court Case API"


@patch("ngm.api.routes.CourtCaseService")
def test_get_court_case_success(mock_service_class, mock_case):
    """Test successful case retrieval."""
    mock_service = Mock()
    mock_service.get_case_detail.return_value = mock_case
    mock_service_class.return_value = mock_service

    response = client.get("/api/ngm/court_case/supreme:081-CR-0081")

    assert response.status_code == 200
    data = response.json()
    assert data["case_number"] == "081-CR-0081"
    assert data["court_identifier"] == "supreme"
    assert data["case_type"] == "भ्रष्टाचार"
    assert len(data["hearings"]) == 1
    assert len(data["entities"]) == 2
    assert data["hearings"][0]["bench"] == "इजलाश 31"
    assert data["entities"][0]["side"] == "plaintiff"
    assert data["entities"][1]["side"] == "defendant"


@patch("ngm.api.routes.CourtCaseService")
def test_get_court_case_not_found(mock_service_class):
    """Test case not found scenario."""
    mock_service = Mock()
    mock_service.get_case_detail.return_value = None
    mock_service_class.return_value = mock_service

    response = client.get("/api/ngm/court_case/supreme:999-XX-9999")

    assert response.status_code == 404
    data = response.json()
    assert "not found" in data["detail"].lower()


def test_get_court_case_invalid_format():
    """Test invalid case_id format."""
    response = client.get("/api/ngm/court_case/invalid-format")

    assert response.status_code == 400
    data = response.json()
    assert "Invalid case_id format" in data["detail"]


def test_get_court_case_missing_case_number():
    """Test case_id with missing case number."""
    response = client.get("/api/ngm/court_case/supreme:")

    assert response.status_code == 400
    data = response.json()
    assert "Invalid case_id format" in data["detail"]


@patch("ngm.api.routes.CourtCaseService")
def test_get_court_case_with_district_court(mock_service_class, mock_case):
    """Test case retrieval for district court."""
    mock_case.court_identifier = "kathmandudc"
    mock_case.case_number = "082-OA-0503"

    mock_service = Mock()
    mock_service.get_case_detail.return_value = mock_case
    mock_service_class.return_value = mock_service

    response = client.get("/api/ngm/court_case/kathmandudc:082-OA-0503")

    assert response.status_code == 200
    data = response.json()
    assert data["case_number"] == "082-OA-0503"
    assert data["court_identifier"] == "kathmandudc"


@patch("ngm.api.routes.CourtCaseService")
def test_get_court_case_no_hearings(mock_service_class, mock_case):
    """Test case with no hearings."""
    mock_case.hearings = []

    mock_service = Mock()
    mock_service.get_case_detail.return_value = mock_case
    mock_service_class.return_value = mock_service

    response = client.get("/api/ngm/court_case/supreme:081-CR-0081")

    assert response.status_code == 200
    data = response.json()
    assert len(data["hearings"]) == 0


@patch("ngm.api.routes.CourtCaseService")
def test_get_court_case_with_lowercase(mock_service_class, mock_case):
    """Test case retrieval with lowercase case number."""
    mock_case.case_number = "081-CR-0081"  # Normalized version

    mock_service = Mock()
    mock_service.get_case_detail.return_value = mock_case
    mock_service_class.return_value = mock_service

    # Request with lowercase
    response = client.get("/api/ngm/court_case/supreme:081-cr-0081")

    assert response.status_code == 200
    data = response.json()
    assert data["case_number"] == "081-CR-0081"
    # Verify service was called with normalized case number
    mock_service.get_case_detail.assert_called_once_with("supreme", "081-CR-0081")


@patch("ngm.api.routes.CourtCaseService")
def test_get_court_case_with_missing_zeros(mock_service_class, mock_case):
    """Test case retrieval with missing leading zeros."""
    mock_case.case_number = "081-CR-0081"  # Normalized version

    mock_service = Mock()
    mock_service.get_case_detail.return_value = mock_case
    mock_service_class.return_value = mock_service

    # Request without leading zeros
    response = client.get("/api/ngm/court_case/supreme:81-cr-81")

    assert response.status_code == 200
    data = response.json()
    assert data["case_number"] == "081-CR-0081"
    # Verify service was called with normalized case number
    mock_service.get_case_detail.assert_called_once_with("supreme", "081-CR-0081")


@patch("ngm.api.routes.CourtCaseService")
def test_get_court_case_with_nepali_numerals(mock_service_class, mock_case):
    """Test case retrieval with Nepali numerals."""
    mock_case.case_number = "081-CR-0081"  # Normalized version

    mock_service = Mock()
    mock_service.get_case_detail.return_value = mock_case
    mock_service_class.return_value = mock_service

    # Request with Nepali numerals
    response = client.get("/api/ngm/court_case/supreme:०८१-CR-००८१")

    assert response.status_code == 200
    data = response.json()
    assert data["case_number"] == "081-CR-0081"
    # Verify service was called with normalized case number
    mock_service.get_case_detail.assert_called_once_with("supreme", "081-CR-0081")


@patch("ngm.api.routes.CourtCaseService")
def test_get_court_case_no_entities(mock_service_class, mock_case):
    """Test case with no entities."""
    mock_case.entities = []

    mock_service = Mock()
    mock_service.get_case_detail.return_value = mock_case
    mock_service_class.return_value = mock_service

    response = client.get("/api/ngm/court_case/supreme:081-CR-0081")

    assert response.status_code == 200
    data = response.json()
    assert len(data["entities"]) == 0
