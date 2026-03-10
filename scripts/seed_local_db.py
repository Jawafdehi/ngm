"""
Seed local database with data from production database.

This script safely copies courts, cases, and hearings from a production database
to a local development database. It includes safety checks to prevent accidental
writes to production.

Usage:
    export DATABASE_URL='postgresql://user:pass@host:5432/prod_db'
    export LOCAL_DATABASE_URL='postgresql://user:pass@localhost:5433/local_db'
    poetry run python scripts/seed_local_db.py --confirm-prod-seed

    # Custom limit
    poetry run python scripts/seed_local_db.py --confirm-prod-seed --limit 100
"""

import argparse
import logging
import os
import sys
from typing import Tuple
from urllib.parse import urlparse

from ngm.database.models import (
    Court,
    CourtCase,
    CourtCaseHearing,
    get_engine,
    get_session,
    init_db,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class SafetyCheckError(Exception):
    """Raised when database safety checks fail to prevent accidental production writes."""

    pass


class DatabaseSeeder:
    """Seed local database from production."""

    def __init__(self, prod_url: str, local_url: str, cases_limit: int = 200):
        if cases_limit <= 0:
            raise ValueError("cases_limit must be positive")

        self.prod_url = prod_url
        self.local_url = local_url
        self.cases_limit = cases_limit

        self._validate_urls()

        self.prod_engine = None
        self.prod_session = None
        self.local_engine = None
        self.local_session = None

        self.stats = {"courts": 0, "cases": 0, "hearings": 0}

    def _validate_urls(self) -> None:
        """Validate prod and local URLs are different using SQLAlchemy URL parsing."""
        from sqlalchemy.engine.url import make_url

        try:
            prod_url_obj = make_url(self.prod_url)
            local_url_obj = make_url(self.local_url)

            # Compare full normalized URLs including query parameters
            # This ensures different endpoints (unix sockets, Cloud SQL proxies, etc.) are detected
            prod_normalized = prod_url_obj.render_as_string(hide_password=False)
            local_normalized = local_url_obj.render_as_string(hide_password=False)

            if prod_normalized == local_normalized:
                raise SafetyCheckError(
                    "Prod and local URLs point to the same database. Aborting."
                )
        except Exception as e:
            if isinstance(e, SafetyCheckError):
                raise
            raise SafetyCheckError(f"Failed to parse database URLs: {e}") from e

    def _connect_databases(self) -> None:
        """Establish connections to production and local databases."""
        self._reset_engine_singleton()
        logger.info("Connecting to production database...")
        self.prod_engine = get_engine(self.prod_url)
        self.prod_session = get_session(self.prod_engine)
        logger.info("Connected to production database")

        logger.info("Connecting to local database...")
        self._reset_engine_singleton()
        self.local_engine = get_engine(self.local_url)
        init_db(self.local_engine)
        self.local_session = get_session(self.local_engine)
        logger.info("Connected to local database")

    @staticmethod
    def _reset_engine_singleton() -> None:
        """Reset the global engine singleton to allow multiple connections."""
        import ngm.database.models as models

        models._engine = None
        models._engine_url = None

    def _cleanup(self) -> None:
        """Close connections and dispose engines."""
        for resource, name in [
            (self.prod_session, "prod_session"),
            (self.local_session, "local_session"),
            (self.prod_engine, "prod_engine"),
            (self.local_engine, "local_engine"),
        ]:
            if resource:
                try:
                    if "session" in name:
                        resource.close()
                    else:
                        resource.dispose()
                except Exception:
                    logger.exception(f"Error cleaning up {name}")
        self._reset_engine_singleton()

    def _seed_courts(self) -> None:
        """Seed all courts from production to local database."""
        logger.info("Seeding courts...")

        with self.prod_session.begin():
            courts = self.prod_session.query(Court).all()
            court_data = [
                {
                    "identifier": c.identifier,
                    "court_type": c.court_type,
                    "full_name_nepali": c.full_name_nepali,
                    "full_name_english": c.full_name_english,
                }
                for c in courts
            ]

        with self.local_session.begin():
            for court in court_data:
                self.local_session.merge(Court(**court))

        self.stats["courts"] = len(court_data)
        logger.info(f"Seeded {len(court_data)} courts")

    def _seed_cases(self) -> list:
        """
        Seed cases from production to local database.

        Returns:
            List of case data dictionaries for use in seeding hearings
        """
        logger.info(
            f"Seeding cases (special court with verdicts, 2080 BS onwards, limit: {self.cases_limit})..."
        )

        with self.prod_session.begin():
            cases = (
                self.prod_session.query(CourtCase)
                .filter(CourtCase.court_identifier == "special")
                .filter(CourtCase.case_status.like("%फैसला%"))
                .filter(CourtCase.registration_date_bs >= "2080")
                .order_by(CourtCase.registration_date_bs.desc())
                .limit(self.cases_limit)
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

        with self.local_session.begin():
            for case in case_data:
                self.local_session.merge(CourtCase(**case))

        self.stats["cases"] = len(case_data)
        logger.info(f"Seeded {len(case_data)} cases")

        return case_data

    def _seed_hearings(self, case_data: list) -> None:
        """Seed hearings for given cases."""
        logger.info("Seeding hearings...")

        case_numbers = [c["case_number"] for c in case_data]
        if not case_numbers:
            return

        with self.prod_session.begin():
            hearings = (
                self.prod_session.query(CourtCaseHearing)
                .filter(CourtCaseHearing.case_number.in_(case_numbers))
                .filter(CourtCaseHearing.court_identifier == "special")
                .all()
            )

            hearing_data = [
                {
                    "id": h.id,
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

        with self.local_session.begin():
            for hearing in hearing_data:
                self.local_session.merge(CourtCaseHearing(**hearing))

            # Update sequence to prevent ID collisions on future inserts
            if hearing_data:
                from sqlalchemy import text

                max_id_result = self.local_session.execute(
                    text("SELECT COALESCE(MAX(id), 0) FROM court_case_hearings")
                ).scalar()

                # Use pg_get_serial_sequence for robustness
                self.local_session.execute(
                    text(
                        "SELECT setval("
                        "pg_get_serial_sequence('court_case_hearings', 'id'), "
                        ":max_id, true)"
                    ),
                    {"max_id": max_id_result},
                )
                logger.info(f"Updated court_case_hearings sequence to {max_id_result}")

        self.stats["hearings"] = len(hearing_data)
        logger.info(f"Seeded {len(hearing_data)} hearings")

    def seed(self) -> None:
        """Execute seeding process."""
        logger.info(
            f"Seeding from {self._mask_url(self.prod_url)} to {self._mask_url(self.local_url)}"
        )
        logger.info(f"Limit: {self.cases_limit} cases")

        try:
            self._connect_databases()
            self._seed_courts()
            case_data = self._seed_cases()
            self._seed_hearings(case_data)

            logger.info(
                f"Seeding complete: {self.stats['courts']} courts, "
                f"{self.stats['cases']} cases, {self.stats['hearings']} hearings"
            )
        finally:
            self._cleanup()

    @staticmethod
    def _mask_url(url: str) -> str:
        """
        Mask sensitive information in database URL.

        Args:
            url: Database connection URL

        Returns:
            Masked URL with credentials hidden
        """
        from urllib.parse import urlunparse

        parsed = urlparse(url)
        if parsed.username or parsed.password:
            # Rebuild netloc with masked credentials
            masked_netloc = f"{parsed.username or ''}:****@{parsed.hostname}"
            if parsed.port:
                masked_netloc += f":{parsed.port}"

            # Reconstruct URL with masked netloc
            return urlunparse(
                (
                    parsed.scheme,
                    masked_netloc,
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )
        return url


def parse_arguments() -> argparse.Namespace:
    """
    Parse command-line arguments.

    Returns:
        Parsed arguments namespace
    """
    parser = argparse.ArgumentParser(
        description="Seed local database with production data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Seed with default limit (200 cases)
  poetry run python scripts/seed_local_db.py --confirm-prod-seed

  # Seed with custom limit
  poetry run python scripts/seed_local_db.py --confirm-prod-seed --limit 100

Environment Variables:
  DATABASE_URL        Production database connection URL (required)
  LOCAL_DATABASE_URL  Local database connection URL (required)
        """,
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Number of cases to seed (default: 200)",
    )

    parser.add_argument(
        "--confirm-prod-seed",
        action="store_true",
        required=True,
        help="Acknowledge that production data will be copied locally (required)",
    )

    return parser.parse_args()


def validate_environment() -> Tuple[str, str]:
    """
    Validate required environment variables are set.

    Returns:
        Tuple of (prod_url, local_url)

    Raises:
        SystemExit: If required environment variables are missing
    """
    prod_url = os.getenv("DATABASE_URL")
    local_url = os.getenv("LOCAL_DATABASE_URL")

    if not prod_url or not local_url:
        logger.error("Missing required environment variables")
        logger.error("")
        logger.error("Both DATABASE_URL and LOCAL_DATABASE_URL must be set:")
        logger.error("  export DATABASE_URL='postgresql://user:pass@host:5432/prod_db'")
        logger.error(
            "  export LOCAL_DATABASE_URL='postgresql://user:pass@localhost:5433/local_db'"
        )
        logger.error("")
        logger.error("Then run:")
        logger.error("  poetry run python scripts/seed_local_db.py --confirm-prod-seed")
        sys.exit(1)

    return prod_url, local_url


def main() -> None:
    """Main entry point for the seeding script."""
    args = parse_arguments()

    # Validate limit is positive
    if args.limit <= 0:
        logger.error("--limit must be a positive integer")
        sys.exit(1)

    prod_url, local_url = validate_environment()

    try:
        seeder = DatabaseSeeder(prod_url, local_url, args.limit)
        seeder.seed()
    except SafetyCheckError as e:
        logger.error(f"Safety check failed: {e}")
        sys.exit(1)
    except ValueError as e:
        logger.error(f"Invalid argument: {e}")
        sys.exit(1)
    except Exception:
        logger.exception("Seeding failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
