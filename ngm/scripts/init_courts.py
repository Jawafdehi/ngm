"""
Initialize all courts in Nepal database.

This script populates the courts table with all district courts, high courts,
supreme court, and special court. Uses "upsert" logic (insert or update).

Usage:
    poetry run python ngm/scripts/init_courts.py

    # With custom database URL
    DATABASE_URL='postgresql://user:pass@host:5432/db' poetry run python ngm/scripts/init_courts.py
"""

import os
import sys

from dotenv import load_dotenv

from sqlalchemy import text
from ngm.database.models import Court, get_engine, get_session, init_db
from ngm.utils.court_ids import DISTRICT_COURTS, HIGH_COURTS


def build_local_courts_db():
    """
    Build a local dictionary of all courts from our data sources.

    Returns:
        dict: Dictionary mapping identifier to court data
    """
    local_courts = {}

    # Supreme Court
    local_courts["supreme"] = {
        "identifier": "supreme",
        "court_type": "supreme",
        "full_name_nepali": "सर्वोच्च अदालत",
        "full_name_english": "Supreme Court",
    }

    # Special Court
    local_courts["special"] = {
        "identifier": "special",
        "court_type": "special",
        "full_name_nepali": "विशेष अदालत",
        "full_name_english": "Special Court",
    }

    # High Courts
    for hc in HIGH_COURTS:
        local_courts[hc["identifier"]] = {
            "identifier": hc["identifier"],
            "court_type": "high",
            "full_name_nepali": hc["name"],
            "full_name_english": hc["name_en"],
        }

    # District Courts
    for dc in DISTRICT_COURTS:
        if dc.get("code_name"):
            local_courts[dc["code_name"]] = {
                "identifier": dc["code_name"],
                "court_type": "district",
                "full_name_nepali": dc["name"],
                "full_name_english": dc["name_en"],
            }

    return local_courts


def needs_update(db_court, local_court):
    """
    Check if a database court needs to be updated.

    Args:
        db_court: Court object from database
        local_court: Court data dictionary from local source

    Returns:
        tuple: (needs_update: bool, changes: list of field names)
    """
    changes = []

    if db_court.court_type != local_court["court_type"]:
        changes.append("court_type")

    if db_court.full_name_nepali != local_court["full_name_nepali"]:
        changes.append("full_name_nepali")

    if db_court.full_name_english != local_court["full_name_english"]:
        changes.append("full_name_english")

    return len(changes) > 0, changes


def init_courts(database_url=None):
    """
    Initialize all courts in the database using upsert logic.

    Args:
        database_url: PostgreSQL connection string. If None, reads from DATABASE_URL env var.
    """
    # Get database connection
    if database_url is None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            print("ERROR: DATABASE_URL environment variable not set")
            print(
                "Usage: DATABASE_URL='postgresql://user:pass@host:5432/db' poetry run python ngm/scripts/init_courts.py"
            )
            sys.exit(1)

    print("Connecting to database...")
    engine = get_engine(database_url)

    # Enable pg_trgm extension for full-text search
    print("Enabling pg_trgm extension...")
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            conn.commit()
        print("   ✓ pg_trgm extension enabled")
    except Exception as e:
        print(f"   ⚠ Warning: Could not enable pg_trgm extension: {e}")
        print("   Full-text search indexes will not be created.")

    # Create tables if they don't exist
    print("Creating tables if they don't exist...")
    init_db(engine)

    session = get_session(engine)

    try:
        # Build local courts database
        print("\nBuilding local courts database...")
        local_courts = build_local_courts_db()
        print(f"   ✓ Built local database with {len(local_courts)} courts")

        # Initialize statistics
        stats = {
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "by_type": {
                "supreme": {"created": 0, "updated": 0, "unchanged": 0},
                "special": {"created": 0, "updated": 0, "unchanged": 0},
                "high": {"created": 0, "updated": 0, "unchanged": 0},
                "district": {"created": 0, "updated": 0, "unchanged": 0},
            },
        }

        # Process each court
        print(f"\nProcessing {len(local_courts)} courts...")
        print("=" * 80)

        for identifier, local_court in local_courts.items():
            court_type = local_court["court_type"]

            # Check if court exists in database
            db_court = session.query(Court).filter_by(identifier=identifier).first()

            if not db_court:
                # Create new court
                db_court = Court(
                    identifier=local_court["identifier"],
                    court_type=local_court["court_type"],
                    full_name_nepali=local_court["full_name_nepali"],
                    full_name_english=local_court["full_name_english"],
                )
                session.add(db_court)
                stats["created"] += 1
                stats["by_type"][court_type]["created"] += 1
                print(f"✓ CREATED   [{court_type:8}] {local_court['full_name_nepali']}")

            else:
                # Check if update is needed
                update_needed, changes = needs_update(db_court, local_court)

                if update_needed:
                    # Update existing court
                    db_court.court_type = local_court["court_type"]
                    db_court.full_name_nepali = local_court["full_name_nepali"]
                    db_court.full_name_english = local_court["full_name_english"]
                    stats["updated"] += 1
                    stats["by_type"][court_type]["updated"] += 1
                    changes_str = ", ".join(changes)
                    print(
                        f"↻ UPDATED   [{court_type:8}] {local_court['full_name_nepali']} ({changes_str})"
                    )
                else:
                    # No changes needed
                    stats["unchanged"] += 1
                    stats["by_type"][court_type]["unchanged"] += 1
                    # Uncomment to see unchanged courts
                    # print(f"- UNCHANGED [{court_type:8}] {local_court['full_name_nepali']}")

        # Commit all changes
        session.commit()

        # Print detailed summary
        print("\n" + "=" * 80)
        print("SUMMARY")
        print("=" * 80)
        print(f"Total courts processed:  {len(local_courts)}")
        print(f"  Created:               {stats['created']}")
        print(f"  Updated:               {stats['updated']}")
        print(f"  Unchanged:             {stats['unchanged']}")
        print("=" * 80)

        # Print breakdown by court type
        print("\nBreakdown by Court Type:")
        print("-" * 80)
        print(
            f"{'Type':<12} {'Created':<10} {'Updated':<10} {'Unchanged':<10} {'Total':<10}"
        )
        print("-" * 80)

        for court_type in ["supreme", "special", "high", "district"]:
            type_stats = stats["by_type"][court_type]
            total = (
                type_stats["created"] + type_stats["updated"] + type_stats["unchanged"]
            )
            print(
                f"{court_type.capitalize():<12} {type_stats['created']:<10} {type_stats['updated']:<10} {type_stats['unchanged']:<10} {total:<10}"
            )

        print("-" * 80)

        # Verify database counts
        print("\nDatabase Verification:")
        print("-" * 80)
        for court_type in ["supreme", "special", "high", "district"]:
            count = session.query(Court).filter_by(court_type=court_type).count()
            print(f"  {court_type.capitalize()}: {count}")

        total_db = session.query(Court).count()
        print(f"  Total: {total_db}")
        print("-" * 80)

        print("\n✓ Court initialization complete!")

    except Exception as e:
        session.rollback()
        print(f"\n✗ ERROR: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    load_dotenv()

    init_courts()
