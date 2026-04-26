"""Tests for the CourtCaseService."""

import pytest
from unittest.mock import Mock, patch
from datetime import date, datetime
from ngm.api.service import CourtCaseService


@pytest.fixture
def mock_session():
    """Create a mock database session."""
    return Mock()


@pytest.fixture
def mock_case():
    """Create a mock court case with hearings and entities."""
    case = Mock()
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
    hearing = Mock()
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
    plaintiff_entity = Mock()
    plaintiff_entity.id = 5678
    plaintiff_entity.side = "plaintiff"
    plaintiff_entity.name = "नेपाल सरकार"
    plaintiff_entity.address = "काठमाडौं"
    plaintiff_entity.nes_id = None

    defendant_entity = Mock()
    defendant_entity.id = 5679
    defendant_entity.side = "defendant"
    defendant_entity.name = "जन बहादुर गुरुङ"
    defendant_entity.address = "पोखरा"
    defendant_entity.nes_id = None

    case.hearings = [hearing]
    case.entities = [plaintiff_entity, defendant_entity]

    return case


def test_service_with_provided_session(mock_session):
    """Test service initialization with provided session."""
    service = CourtCaseService(session=mock_session)
    assert service.session == mock_session
    assert not service._owns_session

    # Should not close provided session
    service.close()
    mock_session.close.assert_not_called()


@patch("ngm.api.service.get_engine")
@patch("ngm.api.service.sessionmaker")
def test_service_creates_own_session(mock_sessionmaker, mock_get_engine):
    """Test service creates its own session when none provided."""
    mock_session = Mock()
    mock_sessionmaker.return_value = Mock(return_value=mock_session)

    service = CourtCaseService()
    assert service.session == mock_session
    assert service._owns_session

    # Should close owned session
    service.close()
    mock_session.close.assert_called_once()


def test_context_manager(mock_session):
    """Test service as context manager."""
    with CourtCaseService(session=mock_session) as service:
        assert service.session == mock_session


def test_get_case_detail_success(mock_session, mock_case):
    """Test successful case retrieval."""
    # Setup mock query chain with options
    mock_query = Mock()
    mock_options = Mock()
    mock_filter = Mock()
    mock_query.options.return_value = mock_options
    mock_options.filter.return_value = mock_filter
    mock_filter.first.return_value = mock_case
    mock_session.query.return_value = mock_query

    # Mock hearings query
    mock_hearings_query = Mock()
    mock_hearings_options = Mock()
    mock_hearings_filter = Mock()
    mock_hearings_order = Mock()
    mock_hearings_query.options.return_value = mock_hearings_options
    mock_hearings_options.filter.return_value = mock_hearings_filter
    mock_hearings_filter.order_by.return_value = mock_hearings_order
    mock_hearings_order.all.return_value = mock_case.hearings

    # Mock entities query
    mock_entities_query = Mock()
    mock_entities_options = Mock()
    mock_entities_filter = Mock()
    mock_entities_query.options.return_value = mock_entities_options
    mock_entities_options.filter.return_value = mock_entities_filter
    mock_entities_filter.all.return_value = mock_case.entities

    # Setup query side effects
    mock_session.query.side_effect = [
        mock_query,  # Case query
        mock_hearings_query,  # Hearings query
        mock_entities_query,  # Entities query
    ]

    service = CourtCaseService(session=mock_session)
    case = service.get_case_detail("supreme", "081-CR-0081")

    assert case is not None
    assert case.case_number == "081-CR-0081"
    assert case.court_identifier == "supreme"
    assert len(case.hearings) == 1
    assert len(case.entities) == 2


def test_get_case_detail_not_found(mock_session):
    """Test case not found scenario."""
    mock_query = Mock()
    mock_options = Mock()
    mock_filter = Mock()
    mock_query.options.return_value = mock_options
    mock_options.filter.return_value = mock_filter
    mock_filter.first.return_value = None
    mock_session.query.return_value = mock_query

    service = CourtCaseService(session=mock_session)
    case = service.get_case_detail("supreme", "999-XX-9999")

    assert case is None


@patch("ngm.api.service.normalize_case_number")
def test_case_number_normalization(mock_normalize, mock_session, mock_case):
    """Test that case numbers are normalized."""
    mock_normalize.return_value = "081-CR-0081"

    # Setup mock query chain with options
    mock_query = Mock()
    mock_options = Mock()
    mock_filter = Mock()
    mock_query.options.return_value = mock_options
    mock_options.filter.return_value = mock_filter
    mock_filter.first.return_value = mock_case
    mock_session.query.return_value = mock_query

    # Mock hearings and entities queries
    mock_hearings_query = Mock()
    mock_hearings_options = Mock()
    mock_hearings_filter = Mock()
    mock_hearings_order = Mock()
    mock_hearings_query.options.return_value = mock_hearings_options
    mock_hearings_options.filter.return_value = mock_hearings_filter
    mock_hearings_filter.order_by.return_value = mock_hearings_order
    mock_hearings_order.all.return_value = []

    mock_entities_query = Mock()
    mock_entities_options = Mock()
    mock_entities_filter = Mock()
    mock_entities_query.options.return_value = mock_entities_options
    mock_entities_options.filter.return_value = mock_entities_filter
    mock_entities_filter.all.return_value = []

    mock_session.query.side_effect = [
        mock_query,
        mock_hearings_query,
        mock_entities_query,
    ]

    service = CourtCaseService(session=mock_session)

    # Test with lowercase
    service.get_case_detail("supreme", "081-cr-0081")
    mock_normalize.assert_called_with("081-cr-0081")

    # Test with missing zeros
    mock_session.query.side_effect = [
        mock_query,
        mock_hearings_query,
        mock_entities_query,
    ]
    service.get_case_detail("supreme", "81-cr-81")
    mock_normalize.assert_called_with("81-cr-81")


def test_no_normalization_when_disabled(mock_session, mock_case):
    """Test that normalization can be disabled."""
    # Setup mock query chain with options
    mock_query = Mock()
    mock_options = Mock()
    mock_filter = Mock()
    mock_query.options.return_value = mock_options
    mock_options.filter.return_value = mock_filter
    mock_filter.first.return_value = mock_case
    mock_session.query.return_value = mock_query

    # Mock hearings and entities queries
    mock_hearings_query = Mock()
    mock_hearings_options = Mock()
    mock_hearings_filter = Mock()
    mock_hearings_order = Mock()
    mock_hearings_query.options.return_value = mock_hearings_options
    mock_hearings_options.filter.return_value = mock_hearings_filter
    mock_hearings_filter.order_by.return_value = mock_hearings_order
    mock_hearings_order.all.return_value = []

    mock_entities_query = Mock()
    mock_entities_options = Mock()
    mock_entities_filter = Mock()
    mock_entities_query.options.return_value = mock_entities_options
    mock_entities_options.filter.return_value = mock_entities_filter
    mock_entities_filter.all.return_value = []

    mock_session.query.side_effect = [
        mock_query,
        mock_hearings_query,
        mock_entities_query,
    ]

    service = CourtCaseService(session=mock_session)

    with patch("ngm.api.service.normalize_case_number") as mock_normalize:
        service.get_case_detail("supreme", "081-CR-0081", normalize=False)
        mock_normalize.assert_not_called()
