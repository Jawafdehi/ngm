"""
Seed local database with data from production database.
Usage:
    export DATABASE_URL=''
    export LOCAL_DATABASE_URL=''
    poetry run python ngm/scripts/seed_local_db.py --confirm-prod-seed

Or with custom limit:
    poetry run python ngm/scripts/seed_local_db.py --confirm-prod-seed --limit 100
"""

import os
import sys
import argparse
from urllib.parse import urlparse
from ngm.database.models import (
    get_engine,
    get_session,
    init_db,
    Court,
    CourtCase,
    CourtCaseHearing,
)


def reset_engine_singleton():
    """Reset the global engine singleton to allow multiple connections."""
    import ngm.database.models as models

    models._engine = None
    models._engine_url = None


def seed_database(prod_url, local_url, cases_limit=200):
    """
    Seed local database with production data.

    Args:
        prod_url: Production database URL (read-only)
        local_url: Local database URL (read-write)
        cases_limit: Number of cases to seed (default: 200)
    """

    # Prevent accidental production writes
    def _db_identity(url: str):
        parsed = urlparse(url)
        return (parsed.hostname, parsed.port, parsed.path)

    if _db_identity(prod_url) == _db_identity(local_url):
        raise ValueError(
            "SAFETY CHECK FAILED: LOCAL_DATABASE_URL points to the same database as DATABASE_URL. "
            "Aborting to prevent accidental production writes."
        )

    print("=" * 60)
    print("NGM Database Seeding")
    print("=" * 60)
    print(f"\nProduction DB: {prod_url.split('@')[1] if '@' in prod_url else prod_url}")
    print(
        f"Local DB:      {local_url.split('@')[1] if '@' in local_url else local_url}"
    )
    print(f"Cases limit:   {cases_limit}\n")

    # ── Connect to production DB ──────────────────────────────────────────
    print("Connecting to production database...")
    prod_engine = get_engine(prod_url)
    prod_session = get_session(prod_engine)
    print(" Connected to production")

    # ── Connect to local DB ───────────────────────────────────────────────
    print("Connecting to local database...")
    reset_engine_singleton()
    local_engine = get_engine(local_url)
    init_db(local_engine)
    local_session = get_session(local_engine)
    print(" Connected to local")
    print()

    # ── Seed Courts ───────────────────────────────────────────────────────
    print("Seeding courts...")
    try:
        with prod_session.begin():
            courts = prod_session.query(Court).all()
            court_data = [
                {
                    "identifier": c.identifier,
                    "court_type": c.court_type,
                    "full_name_nepali": c.full_name_nepali,
                    "full_name_english": c.full_name_english,
                }
                for c in courts
            ]

        with local_session.begin():
            for c in court_data:
                local_session.merge(Court(**c))

        print(f"   Seeded {len(court_data)} courts")
    except Exception as e:
        print(f"  ✗ Error seeding courts: {e}")
        raise

    # ── Seed Cases ────────────────────────────────────────────────────────
    print("\nSeeding cases (special court with verdicts)...")
    try:
        with prod_session.begin():
            cases = (
                prod_session.query(CourtCase)
                .filter(CourtCase.court_identifier == "special")
                .filter(CourtCase.case_status.like("%फैसला%"))
                .order_by(CourtCase.registration_date_bs.desc())
                .limit(cases_limit)
                .all()
            )

            case_data = [
                {
                    "case_number": c.case_number,
                    "court_identifier": c.court_identifier,
                    "registration_date_bs": c.registration_date_bs,
                    "registration_date_ad": c.registration_date_ad,
                    "case_type": c.case_type,
                    "division": c.division,
                    "category": c.category,
                    "section": c.section,
                    "plaintiff": c.plaintiff,
                    "defendant": c.defendant,
                    "original_case_number": c.original_case_number,
                    "case_id": c.case_id,
                    "priority": c.priority,
                    "registration_number": c.registration_number,
                    "case_status": c.case_status,
                    "verdict_date_bs": c.verdict_date_bs,
                    "verdict_date_ad": c.verdict_date_ad,
                    "verdict_judge": c.verdict_judge,
                    "status": c.status,
                    "extra_data": c.extra_data,
                }
                for c in cases
            ]

        with local_session.begin():
            for c in case_data:
                local_session.merge(CourtCase(**c))

        print(f"   Seeded {len(case_data)} cases")
    except Exception as e:
        print(f"  ✗ Error seeding cases: {e}")
        raise

    # ── Seed Hearings ─────────────────────────────────────────────────────
    print("\nSeeding hearings...")
    try:
        case_numbers = [c["case_number"] for c in case_data]

        with prod_session.begin():
            hearings = (
                prod_session.query(CourtCaseHearing)
                .filter(CourtCaseHearing.case_number.in_(case_numbers))
                .filter(CourtCaseHearing.court_identifier == "special")
                .all()
            )

            hearing_data = [
                {
                    "case_number": h.case_number,
                    "court_identifier": h.court_identifier,
                    "hearing_date_bs": h.hearing_date_bs,
                    "hearing_date_ad": h.hearing_date_ad,
                    "bench": h.bench,
                    "bench_type": h.bench_type,
                    "judge_names": h.judge_names,
                    "lawyer_names": h.lawyer_names,
                    "serial_no": h.serial_no,
                    "case_status": h.case_status,
                    "decision_type": h.decision_type,
                    "remarks": h.remarks,
                    "scraped_at": h.scraped_at,
                    "extra_data": h.extra_data,
                }
                for h in hearings
            ]

        with local_session.begin():
            for h in hearing_data:
                local_session.merge(CourtCaseHearing(**h))

        print(f"   Seeded {len(hearing_data)} hearings")
    except Exception as e:
        print(f"  ✗ Error seeding hearings: {e}")
        raise

    # ── Cleanup ───────────────────────────────────────────────────────────
    print("\nCleaning up connections...")
    prod_session.close()
    local_session.close()
    prod_engine.dispose()
    local_engine.dispose()

    print()
    print("=" * 60)
    print(" Done! Local database seeded successfully.")
    print("=" * 60)
    print()
    print("Summary:")
    print(f"  - Courts: {len(court_data)}")
    print(f"  - Cases: {len(case_data)}")
    print(f"  - Hearings: {len(hearing_data)}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Seed local database with production data"
    )
    parser.add_argument(
        "--limit", type=int, default=200, help="Number of cases to seed (default: 200)"
    )
    parser.add_argument(
        "--confirm-prod-seed",
        action="store_true",
        help="Acknowledge that production data will be copied locally (required)",
    )

    args = parser.parse_args()

    prod_url = os.getenv("DATABASE_URL")
    local_url = os.getenv("LOCAL_DATABASE_URL")

    if not prod_url or not local_url:
        print("ERROR: Both DATABASE_URL and LOCAL_DATABASE_URL must be set")
        print()
        print("Example:")
        print(
            "export DATABASE_URL='postgresql://<user>:<password>@<host>:5432/<dbname>'"
        )
        print(
            "export LOCAL_DATABASE_URL='postgresql://<user>:<password>@localhost:5433/<dbname>'"
        )
        print("poetry run python ngm/scripts/seed_local_db.py --confirm-prod-seed")
        sys.exit(1)

    if not args.confirm_prod_seed:
        print(
            "ERROR: Refusing to seed from production without --confirm-prod-seed flag"
        )
        print(
            "This is a safety measure to prevent accidental copying of production data."
        )
        print()
        print("To proceed, run:")
        print("poetry run python ngm/scripts/seed_local_db.py --confirm-prod-seed")
        sys.exit(1)

    try:
        seed_database(prod_url, local_url, args.limit)
    except Exception as e:
        print(f"\nSeeding failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
