import os
from sqlalchemy import create_engine, text
import sys


def check():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set")
        sys.exit(1)

    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            # Check if we have cases with null case_type or other expected fields
            # suggesting a selector failure (schema drift)
            result = conn.execute(
                text(
                    "SELECT count(*) FROM court_cases WHERE case_type IS NULL OR case_type = ''"
                )
            )
            count = result.scalar()
            if count > 10:  # Threshold for drift detection
                print(
                    f"ERROR: Detected {count} cases with missing case_type. Possible schema drift!"
                )
                sys.exit(1)
            print("Schema check passed")
    except Exception as e:
        print(f"Schema check failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    check()
