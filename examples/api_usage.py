#!/usr/bin/env python
"""Example demonstrating how to use the NGM library to query court cases."""

from ngm.api import CourtCaseService


def print_case_summary(case):
    """Print a formatted summary of case data."""
    print("\n" + "=" * 80)
    print(f"CASE: {case.case_number} ({case.court_identifier})")
    print("=" * 80)

    print(f"\nCase Type: {case.case_type or 'N/A'}")
    print(f"Status: {case.case_status or 'N/A'}")
    print(
        f"Registration Date: {case.registration_date_bs or 'N/A'} ({case.registration_date_ad or 'N/A'})"
    )

    if case.plaintiff:
        print(f"\nPlaintiff: {case.plaintiff}")
    if case.defendant:
        print(f"Defendant: {case.defendant}")

    # Print entities
    if case.entities:
        print(f"\n--- Entities ({len(case.entities)}) ---")
        plaintiffs = [e for e in case.entities if e.side == "plaintiff"]
        defendants = [e for e in case.entities if e.side == "defendant"]

        if plaintiffs:
            print("\nPlaintiffs:")
            for entity in plaintiffs:
                address = f" ({entity.address})" if entity.address else ""
                print(f"  - {entity.name}{address}")

        if defendants:
            print("\nDefendants:")
            for entity in defendants:
                address = f" ({entity.address})" if entity.address else ""
                print(f"  - {entity.name}{address}")

    # Print hearings
    if case.hearings:
        print(f"\n--- Hearings ({len(case.hearings)}) ---")
        for i, hearing in enumerate(case.hearings[:5], 1):  # Show first 5 hearings
            print(f"\n{i}. {hearing.hearing_date_bs} ({hearing.hearing_date_ad})")
            if hearing.bench:
                print(f"   Bench: {hearing.bench}")
            if hearing.bench_type:
                print(f"   Type: {hearing.bench_type}")
            if hearing.judge_names:
                print(f"   Judge: {hearing.judge_names}")
            if hearing.case_status:
                print(f"   Status: {hearing.case_status}")

        if len(case.hearings) > 5:
            print(f"\n   ... and {len(case.hearings) - 5} more hearings")

    print("\n" + "=" * 80 + "\n")


def main():
    """Main example function."""

    # Use context manager for automatic cleanup
    with CourtCaseService() as service:
        # Example 1: Fetch a Supreme Court case
        print("Example 1: Fetching Supreme Court case")
        case = service.get_case_detail("supreme", "081-CR-0081")
        if case:
            print_case_summary(case)
        else:
            print("Case not found\n")

        # Example 2: Fetch a District Court case
        print("Example 2: Fetching District Court case")
        case = service.get_case_detail("kathmandudc", "082-OA-0503")
        if case:
            print_case_summary(case)
        else:
            print("Case not found\n")

        # Example 3: Try to fetch a non-existent case
        print("Example 3: Fetching non-existent case")
        case = service.get_case_detail("supreme", "999-XX-9999")
        if not case:
            print("Case not found (expected behavior)\n")

        # Example 4: Analyze case data
        print("Example 4: Analyzing case data")
        case = service.get_case_detail("special", "080-CR-0001")
        if case:
            print(f"Case {case.case_number} has:")
            print(f"  - {len(case.hearings)} hearings")
            print(f"  - {len(case.entities)} entities")

            if case.hearings:
                first_hearing = case.hearings[-1]  # Oldest hearing (list is desc order)
                last_hearing = case.hearings[0]  # Most recent hearing
                print(
                    f"  - First hearing: {first_hearing.hearing_date_bs} ({first_hearing.hearing_date_ad})"
                )
                print(
                    f"  - Last hearing: {last_hearing.hearing_date_bs} ({last_hearing.hearing_date_ad})"
                )
        else:
            print("Case not found\n")

        # Example 5: Case number normalization
        print("\nExample 5: Case number normalization")
        print("-" * 50)

        test_formats = [
            ("081-CR-0081", "Standard format"),
            ("081-cr-0081", "Lowercase"),
            ("81-cr-81", "Without leading zeros"),
            ("०८१-CR-००८१", "Nepali numerals"),
        ]

        for case_num, description in test_formats:
            case = service.get_case_detail("supreme", case_num)
            status = "✓ Found" if case else "✗ Not found"
            print(f"{status}: '{case_num}' ({description})")


if __name__ == "__main__":
    main()
