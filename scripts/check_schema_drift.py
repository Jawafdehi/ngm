import os
import sys
from sqlalchemy import text
from ngm.database.models import get_engine

# A blank case_type only signals selector/schema drift for *litigation* cases.
# Non-litigation registrations (marriage, power-of-attorney, guardianship,
# adoption, death-declaration) are filed under a numeric case-number namespace
# (e.g. 082-02-0001) and legitimately carry no case_type. Litigation cases use
# an alphabetic namespace (CP, FN, CR, WO, C1, ...). We therefore only count
# blanks among alpha-coded rows, and fail on the *ratio* rather than an absolute
# count so the check scales with table size.
LITIGATION_ONLY = "split_part(case_number, '-', 2) ~ '[A-Za-z]'"

# Fail if more than this fraction of in-scope litigation rows lack a case_type.
DRIFT_RATIO_THRESHOLD = 0.02


def check(courts=None):
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set")
        sys.exit(1)

    where = [LITIGATION_ONLY]
    params = {}
    if courts:
        where.append("court_identifier = ANY(:courts)")
        params["courts"] = list(courts)
    scope = " AND ".join(where)

    engine = get_engine(db_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT count(*) AS total, "
                    f"count(*) FILTER (WHERE case_type IS NULL OR case_type = '') AS missing "
                    f"FROM court_cases WHERE {scope}"
                ),
                params,
            ).one()
            total, missing = row.total, row.missing

            scope_label = f"courts={list(courts)}" if courts else "all courts"
            if total == 0:
                print(f"Schema check skipped: no litigation rows in scope ({scope_label})")
                return

            ratio = missing / total
            if ratio > DRIFT_RATIO_THRESHOLD:
                print(
                    f"ERROR: {missing}/{total} ({ratio:.1%}) litigation cases missing "
                    f"case_type ({scope_label}), above {DRIFT_RATIO_THRESHOLD:.0%} "
                    f"threshold. Possible schema drift!"
                )
                sys.exit(1)
            print(
                f"Schema check passed: {missing}/{total} ({ratio:.2%}) litigation "
                f"cases missing case_type ({scope_label})"
            )
    except Exception as e:
        print(f"Schema check failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    # Optional positional args scope the check to specific court identifiers
    # (e.g. `python scripts/check_schema_drift.py supreme special`).
    check(courts=sys.argv[1:] or None)
