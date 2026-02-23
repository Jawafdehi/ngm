# Local Database Seeding Guide

This guide walks you through seeding your local NGM database with production data for development and testing.

## Prerequisites

- Python 3.12+ and Poetry installed
- Project dependencies installed (`poetry install` in the ngm service directory)
- A running PostgreSQL server (pgAdmin, Docker Desktop, Colima, etc.)
- Read-only access to the production database

---

## Step 1 — Make Sure PostgreSQL Is Running

Make sure your PostgreSQL server is started and accessible. The default setup uses:

- **Host:** `localhost`
- **Port:** `5433` (use `5432` if nothing else is using it)
- **Database:** `ngm_local`
- **User:** `ngm`
- **Password:** `ngm_local`

If you haven't created the database yet, create it with these credentials via your preferred tool (pgAdmin, psql, etc.), then enable the required extension:

```bash
psql -U ngm -h localhost -p 5433 -d ngm_local \
  -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
```

---

## Step 2 — Create Tables

Navigate to the ngm service directory and run:

```bash
cd ~/path/to/ngm

DATABASE_URL=postgresql://ngm:ngm_local@localhost:5433/ngm_local poetry run python -c "
from ngm.database.models import get_engine, init_db
engine = get_engine('postgresql://ngm:ngm_local@localhost:5433/ngm_local')
init_db(engine)
print('Tables created!')
"
```

You should see `Tables created!` — if you get a `gin_trgm_ops` error, make sure you ran the extension command in Step 1.

---


## Step 3 — Set Environment Variables

**Database URL Format:**
```
postgresql://username:password@host:port/database_name
```

```bash
export DATABASE_URL='postgresql://<user>:<pass>@<host>:<port>/<db>'
export LOCAL_DATABASE_URL='postgresql://ngm:ngm_local@localhost:5433/ngm_local'
```


## Step 4 — Run the Seeding Script

```bash
# Default (50 cases)
poetry run python ngm/scripts/seed_local_db.py

# Custom limit
poetry run python ngm/scripts/seed_local_db.py --limit 200
```

---

## What Gets Seeded

1. **All Courts** - Complete list of courts in Nepal
2. **Cases** - Special court cases with verdicts (default: 50, customizable with `--limit`)
3. **Hearings** - All hearing records for the seeded cases

## Verify the Data

Connect to your local database and run:

```sql
SELECT COUNT(*) FROM courts;
SELECT COUNT(*) FROM court_cases;
SELECT COUNT(*) FROM court_case_hearings;

-- Sample cases
SELECT case_number, case_type, case_status
FROM court_cases
LIMIT 10;
```

---


## Troubleshooting

**0 cases seeded**
The production DB stores verdict dates inside `case_status` as `"फैसला (मिती: ...)"` rather than in `verdict_date_bs`. The seed script filters by `case_status LIKE '%फैसला%'` to handle this.

**`gin_trgm_ops` error when creating tables**
Run this against your local database:

```bash
psql -U ngm -h localhost -p 5433 -d ngm_local \
  -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
```

**`Both DATABASE_URL and LOCAL_DATABASE_URL must be set`**
Make sure you exported both variables before running the script. Verify with:
```bash
echo $DATABASE_URL
echo $LOCAL_DATABASE_URL
```

**`permission denied for table court_cases`**
Your `DATABASE_URL` is pointing at the production DB which is read-only. Make sure your spider uses `LOCAL_DATABASE_URL` for local testing.

---

## Re-seeding

The script uses upsert logic — safe to run multiple times. Existing records are updated, new ones inserted, no duplicates created.

To start completely fresh, drop and recreate your local database, then repeat from Step 2.
