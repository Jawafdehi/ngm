"""Integration tests for the NGM Court Case API.

These tests require a running database with actual data.
Run with: pytest tests/test_api_integration.py -v

Set SKIP_INTEGRATION_TESTS=1 to skip these tests.
"""

import os
import pytest
from fastapi.testclient import TestClient
from unittest.mock import Mock
from ngm.api.app import app
from ngm.api.routes import get_db

# Skip integration tests if environment variable is set
pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_INTEGRATION_TESTS") == "1",
    reason="Integration tests skipped (SKIP_INTEGRATION_TESTS=1)",
)


# Mock database dependency for tests that don't need real database
def override_get_db():
    """Override database dependency for testing."""
    return Mock()


# Clear any existing dependency overrides first
app.dependency_overrides.clear()

# Only override dependency if DATABASE_URL is not set
# This allows tests to use real database when DATABASE_URL is available
if not os.environ.get("DATABASE_URL"):
    app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)


def test_health_check_integration():
    """Test the health check endpoint."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


def test_api_docs_available():
    """Test that API documentation is accessible."""
    response = client.get("/docs")
    assert response.status_code == 200


def test_openapi_schema():
    """Test that OpenAPI schema is available."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "openapi" in schema
    assert "paths" in schema
    assert "/api/ngm/court_case/{case_id}" in schema["paths"]


def test_invalid_case_id_format():
    """Test various invalid case_id formats."""
    invalid_ids = [
        ("invalid", 400),
        ("no-colon", 400),
        ("supreme", 400),
        (":081-CR-0081", 400),
        ("supreme:", 400),
        ("", 404),  # Empty string doesn't match route, returns 404
    ]

    for case_id, expected_status in invalid_ids:
        response = client.get(f"/api/ngm/court_case/{case_id}")
        assert (
            response.status_code == expected_status
        ), f"Expected {expected_status} for case_id: '{case_id}', got {response.status_code}"
        if expected_status == 400:
            data = response.json()
            assert "Invalid case_id format" in data["detail"]


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set",
)
def test_get_case_with_real_database():
    """
    Test case retrieval with real database.

    This test will pass if:
    1. Database is accessible
    2. Either the case exists (200) or doesn't exist (404)

    It will fail if there's a server error (500).
    """
    # Try to fetch a case (may or may not exist)
    response = client.get("/api/ngm/court_case/supreme:081-CR-0081")

    # Should be either 200 (found) or 404 (not found), not 500 (error)
    assert response.status_code in [200, 404]

    if response.status_code == 200:
        data = response.json()
        # Verify response structure
        assert "case_number" in data
        assert "court_identifier" in data
        assert "hearings" in data
        assert "entities" in data
        assert isinstance(data["hearings"], list)
        assert isinstance(data["entities"], list)

        # Verify the case matches what we requested
        assert data["case_number"] == "081-CR-0081"
        assert data["court_identifier"] == "supreme"


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set",
)
def test_case_response_structure():
    """
    Test that case response has correct structure.

    Uses a known case or skips if no cases exist.
    """
    # Try multiple common case patterns
    test_cases = [
        "supreme:081-CR-0081",
        "supreme:080-CR-0001",
        "special:080-CR-0001",
    ]

    case_found = False
    for case_id in test_cases:
        response = client.get(f"/api/ngm/court_case/{case_id}")
        if response.status_code == 200:
            case_found = True
            data = response.json()

            # Verify all required fields are present
            required_fields = [
                "case_number",
                "court_identifier",
                "created_at",
                "updated_at",
                "hearings",
                "entities",
            ]
            for field in required_fields:
                assert field in data, f"Missing required field: {field}"

            # Verify data types
            assert isinstance(data["hearings"], list)
            assert isinstance(data["entities"], list)

            # If hearings exist, verify hearing structure
            if data["hearings"]:
                hearing = data["hearings"][0]
                assert "id" in hearing
                assert "hearing_date_bs" in hearing
                assert "hearing_date_ad" in hearing

            # If entities exist, verify entity structure
            if data["entities"]:
                entity = data["entities"][0]
                assert "id" in entity
                assert "side" in entity
                assert entity["side"] in ["plaintiff", "defendant"]

            break

    if not case_found:
        pytest.skip("No test cases found in database")


def test_cors_headers():
    """Test that CORS headers are present."""
    response = client.get("/", headers={"Origin": "http://example.com"})
    # CORS headers should be present when Origin header is sent
    assert "access-control-allow-origin" in response.headers


def test_case_id_with_special_characters():
    """Test case_id parsing with various formats."""
    # This test needs mocking since we don't have DATABASE_URL
    from unittest.mock import patch, Mock

    with patch("ngm.api.routes.CourtCaseService") as mock_service_class:
        mock_service = Mock()
        mock_service.get_case_detail.return_value = None  # Case not found
        mock_service_class.return_value = mock_service

        # Valid format with colon in case number (should still work)
        response = client.get("/api/ngm/court_case/supreme:081-CR-0081")
        # Should be 404 (not found) since we mocked it to return None
        assert response.status_code == 404
