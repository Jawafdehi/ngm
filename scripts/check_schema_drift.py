import os
import sys

from sqlalchemy import text

from ngm.database.models import get_engine

# A blank case_type only signals selector/schema drift for *litigation* cases.
# Non-litigation registrations (marriage, power-of-attorney, guardianship,
# adoption, death-declaration) are filed under a numeric case-number namespace
# (e.g. 082-02-0001) and legitimately carry no case_type. Litigation cases use
# an alphabetic namespace (CP, FN, CR, WO, C1, ...). We therefore only count
# blanks among alpha-coded rows.
LITIGATION_ONLY = "split_part(case_number, '-', 2) ~ '[A-Za-z]'"

# Only look at rows ingested recently: drift means *this* scrape stopped
# capturing case_type, so a ratio over the whole table history would dilute a
# fresh failure into insignificance (and needlessly scan millions of rows).
RECENT_WINDOW_DAYS = 3

# Fail if more than this fraction of recent litigation rows lack a case_type.
DRIFT_RATIO_THRESHOLD = 0.02

# Don't fail on a handful of rows: a tiny recent sample makes the ratio noisy.
MIN_SAMPLE = 20


def _validate_courts(conn, courts):
    """Error out on unknown court identifiers so a typo can't silently pass."""
    known = {
        r.court_identifier
        for r in conn.execute(
            text(
                "SELECT DISTINCT court_identifier FROM court_cases "
                "WHERE court_identifier = ANY(:courts)"
            ),
            {"courts": list(courts)},
        )
    }
    unknown = sorted(set(courts) - known)
    if unknown:
        print(f"ERROR: unknown court identifier(s): {', '.join(unknown)}")
        sys.exit(1)


def check(courts=None):
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set")
        sys.exit(1)

    where = [
        LITIGATION_ONLY,
        "created_at >= now() - (:days * interval '1 day')",
    ]
    params = {"days": RECENT_WINDOW_DAYS}
    if courts:
        where.append("court_identifier = ANY(:courts)")
        params["courts"] = list(courts)
    scope = " AND ".join(where)

    engine = get_engine(db_url)
    try:
        with engine.connect() as conn:
            if courts:
                _validate_courts(conn, courts)

            row = conn.execute(
                text(
                    f"SELECT count(*) AS total, "
                    f"count(*) FILTER (WHERE case_type IS NULL OR case_type = '') "
                    f"AS missing FROM court_cases WHERE {scope}"
                ),
                params,
            ).one()
            total, missing = row.total, row.missing

            scope_label = f"courts={list(courts)}" if courts else "all courts"
            window_label = f"last {RECENT_WINDOW_DAYS}d, {scope_label}"
            if total < MIN_SAMPLE:
                print(
                    f"Schema check skipped: only {total} recent litigation rows "
                    f"({window_label}), below sample floor {MIN_SAMPLE}"
                )
                return

            ratio = missing / total
            if ratio > DRIFT_RATIO_THRESHOLD:
                print(
                    f"ERROR: {missing}/{total} ({ratio:.1%}) recent litigation cases "
                    f"missing case_type ({window_label}), above "
                    f"{DRIFT_RATIO_THRESHOLD:.0%} threshold. Possible schema drift!"
                )
                sys.exit(1)
            print(
                f"Schema check passed: {missing}/{total} ({ratio:.2%}) recent "
                f"litigation cases missing case_type ({window_label})"
            )
    except Exception as e:
        print(f"Schema check failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    # Optional positional args scope the check to specific court identifiers
    # (e.g. `python scripts/check_schema_drift.py supreme special`).
    check(courts=sys.argv[1:] or None)
