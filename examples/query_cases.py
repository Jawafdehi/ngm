#!/usr/bin/env python
"""Example script demonstrating how to query court cases using the NGM library."""

from ngm.api import CourtCaseService


def main():
    """Query court cases and display information."""

    # Example 1: Using context manager (recommended)
    print("Example 1: Query a Supreme Court case")
    print("-" * 50)

    with CourtCaseService() as service:
        case = service.get_case_detail("supreme", "081-CR-0081")

        if case:
            print(f"Case Number: {case.case_number}")
            print(f"Court: {case.court_identifier}")
            print(f"Case Type: {case.case_type}")
            print(f"Registration Date (BS): {case.registration_date_bs}")
            print(f"Registration Date (AD): {case.registration_date_ad}")
            print(f"Plaintiff: {case.plaintiff}")
            print(f"Defendant: {case.defendant}")
            print(f"Status: {case.case_status}")
            print(f"\nNumber of Hearings: {len(case.hearings)}")
            print(f"Number of Entities: {len(case.entities)}")

            if case.hearings:
                print("\nLatest Hearing:")
                hearing = case.hearings[0]
                print(f"  Date (BS): {hearing.hearing_date_bs}")
                print(f"  Date (AD): {hearing.hearing_date_ad}")
                print(f"  Bench: {hearing.bench}")
                print(f"  Judge: {hearing.judge_names}")
                print(f"  Status: {hearing.case_status}")
        else:
            print("Case not found")

    print("\n" + "=" * 50 + "\n")

    # Example 2: Query with different case number formats
    print("Example 2: Case number normalization")
    print("-" * 50)

    with CourtCaseService() as service:
        # All these formats will be normalized to the same case
        formats = [
            "081-CR-0081",  # Standard format
            "081-cr-0081",  # Lowercase
            "81-cr-81",  # Without leading zeros
            "०८१-CR-००८१",  # Nepali numerals
        ]

        for case_num in formats:
            case = service.get_case_detail("supreme", case_num)
            if case:
                print(f"✓ '{case_num}' → Found: {case.case_number}")
            else:
                print(f"✗ '{case_num}' → Not found")

    print("\n" + "=" * 50 + "\n")

    # Example 3: Query a district court case
    print("Example 3: Query a District Court case")
    print("-" * 50)

    with CourtCaseService() as service:
        case = service.get_case_detail("kathmandudc", "082-OA-0503")

        if case:
            print(f"Case Number: {case.case_number}")
            print(f"Court: {case.court_identifier}")
            print(f"Plaintiff: {case.plaintiff}")
            print(f"Defendant: {case.defendant}")

            if case.entities:
                print("\nParties:")
                for entity in case.entities:
                    print(f"  {entity.side.title()}: {entity.name}")
                    if entity.address:
                        print(f"    Address: {entity.address}")
        else:
            print("Case not found")

    print("\n" + "=" * 50 + "\n")

    # Example 4: Manual session management
    print("Example 4: Manual session management")
    print("-" * 50)

    service = CourtCaseService()
    try:
        case = service.get_case_detail("special", "080-CR-0001")
        if case:
            print(f"Special Court Case: {case.case_number}")
            print(f"Type: {case.case_type}")
        else:
            print("Case not found")
    finally:
        service.close()


if __name__ == "__main__":
    main()
