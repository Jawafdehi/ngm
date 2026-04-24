#!/usr/bin/env python
"""Example script demonstrating NGM Court Case API usage."""

import requests
from typing import Optional


class NGMCourtCaseClient:
    """Simple client for the NGM Court Case API."""

    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_case(self, court: str, case_number: str) -> Optional[dict]:
        """
        Retrieve case details.

        Args:
            court: Court identifier (e.g., 'supreme', 'kathmandudc')
            case_number: Case number (e.g., '081-CR-0081')

        Returns:
            Case data dictionary or None if not found
        """
        case_id = f"{court}:{case_number}"
        url = f"{self.base_url}/api/ngm/court_case/{case_id}"

        try:
            response = requests.get(url, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                print(f"Case not found: {case_id}")
                return None
            raise
        except requests.exceptions.RequestException as e:
            print(f"Error fetching case: {e}")
            return None

    def health_check(self) -> bool:
        """Check if the API is running."""
        try:
            response = requests.get(f"{self.base_url}/", timeout=self.timeout)
            return response.status_code == 200
        except requests.exceptions.RequestException:
            return False


def print_case_summary(case_data: dict):
    """Print a formatted summary of case data."""
    print("\n" + "=" * 80)
    print(f"CASE: {case_data['case_number']} ({case_data['court_identifier']})")
    print("=" * 80)

    print(f"\nCase Type: {case_data.get('case_type', 'N/A')}")
    print(f"Status: {case_data.get('case_status', 'N/A')}")
    print(
        f"Registration Date: {case_data.get('registration_date_bs', 'N/A')} ({case_data.get('registration_date_ad', 'N/A')})"
    )

    if case_data.get("plaintiff"):
        print(f"\nPlaintiff: {case_data['plaintiff']}")
    if case_data.get("defendant"):
        print(f"Defendant: {case_data['defendant']}")

    # Print entities
    entities = case_data.get("entities", [])
    if entities:
        print(f"\n--- Entities ({len(entities)}) ---")
        plaintiffs = [e for e in entities if e["side"] == "plaintiff"]
        defendants = [e for e in entities if e["side"] == "defendant"]

        if plaintiffs:
            print("\nPlaintiffs:")
            for entity in plaintiffs:
                address = f" ({entity['address']})" if entity.get("address") else ""
                print(f"  - {entity['name']}{address}")

        if defendants:
            print("\nDefendants:")
            for entity in defendants:
                address = f" ({entity['address']})" if entity.get("address") else ""
                print(f"  - {entity['name']}{address}")

    # Print hearings
    hearings = case_data.get("hearings", [])
    if hearings:
        print(f"\n--- Hearings ({len(hearings)}) ---")
        for i, hearing in enumerate(hearings[:5], 1):  # Show first 5 hearings
            print(f"\n{i}. {hearing['hearing_date_bs']} ({hearing['hearing_date_ad']})")
            if hearing.get("bench"):
                print(f"   Bench: {hearing['bench']}")
            if hearing.get("bench_type"):
                print(f"   Type: {hearing['bench_type']}")
            if hearing.get("judge_names"):
                print(f"   Judge: {hearing['judge_names']}")
            if hearing.get("case_status"):
                print(f"   Status: {hearing['case_status']}")

        if len(hearings) > 5:
            print(f"\n   ... and {len(hearings) - 5} more hearings")

    print("\n" + "=" * 80 + "\n")


def main():
    """Main example function."""
    # Initialize client
    client = NGMCourtCaseClient()

    # Check if API is running
    print("Checking API health...")
    if not client.health_check():
        print("ERROR: API is not running. Please start it with:")
        print("  poetry run python scripts/run_api.py")
        return

    print("API is running!\n")

    # Example 1: Fetch a Supreme Court case
    print("Example 1: Fetching Supreme Court case")
    case = client.get_case("supreme", "081-CR-0081")
    if case:
        print_case_summary(case)

    # Example 2: Fetch a District Court case
    print("Example 2: Fetching District Court case")
    case = client.get_case("kathmandudc", "082-OA-0503")
    if case:
        print_case_summary(case)

    # Example 3: Try to fetch a non-existent case
    print("Example 3: Fetching non-existent case")
    case = client.get_case("supreme", "999-XX-9999")
    if not case:
        print("(Expected behavior: case not found)\n")

    # Example 4: Analyze case data
    print("Example 4: Analyzing case data")
    case = client.get_case("special", "080-CR-0001")
    if case:
        hearings = case.get("hearings", [])
        entities = case.get("entities", [])

        print(f"Case {case['case_number']} has:")
        print(f"  - {len(hearings)} hearings")
        print(f"  - {len(entities)} entities")

        if hearings:
            first_hearing = hearings[-1]  # Oldest hearing (list is desc order)
            last_hearing = hearings[0]  # Most recent hearing
            print(
                f"  - First hearing: {first_hearing['hearing_date_bs']} ({first_hearing['hearing_date_ad']})"
            )
            print(
                f"  - Last hearing: {last_hearing['hearing_date_bs']} ({last_hearing['hearing_date_ad']})"
            )


if __name__ == "__main__":
    main()
