"""Backfill normalised verdict fields onto existing court_cases rows.

Applies the SAME shared parser the spiders now use (``ngm.utils.case_status_parser``)
to the ~1.6M already-scraped rows, so history and freshly-scraped data are
normalised identically. Fixes on existing data:

- DQ-01: clears the ~103k rows whose ``case_status`` is the scraped column header;
- DQ-02: sets ``verdict_type`` from the outcome enum, or the final decisive hearing;
- DQ-03: fills ``verdict_date_bs/ad`` from the paren form (never overwriting a value).

The parsed lifecycle/raw-outcome are stashed in ``extra_data['parsed_status']``
(JSONB, no schema change) until the typed columns are added in a coordinated DDL.

SAFETY: dry-run by default. It never writes unless BOTH ``--execute`` and
``--i-understand-this-writes-prod`` are passed. Prints a before/after tally.
"""

import argparse
import logging

from sqlalchemy import and_, or_
from sqlalchemy.orm.attributes import flag_modified

from ngm.database.models import CourtCase, get_session, get_engine
from ngm.utils.case_status_parser import (
    is_status_artifact,
    parse_case_status,
    verdict_from_hearings,
)

logger = logging.getLogger(__name__)


def compute_case_updates(case_status, verdict_type, verdict_date_bs, verdict_date_ad, hearings):
    """Pure transform: return (column_updates, parsed_meta) for one row.

    ``column_updates`` holds only the columns whose value should change.
    ``parsed_meta`` is the dict to stash under extra_data['parsed_status'].
    No DB access — unit-tested directly.
    """
    parsed = parse_case_status(case_status)
    updates = {}

    # DQ-01 — a stored header artifact is not a real status.
    if is_status_artifact(case_status):
        updates["case_status"] = None

    # DQ-02 — outcome enum from the status, else the final decisive hearing.
    new_verdict = parsed.verdict_type or verdict_from_hearings(hearings)
    if new_verdict and new_verdict != verdict_type:
        updates["verdict_type"] = new_verdict

    # DQ-03 — fill a missing verdict date from the paren form; never overwrite.
    if parsed.verdict_date_bs and not verdict_date_bs:
        updates["verdict_date_bs"] = parsed.verdict_date_bs
        updates["verdict_date_ad"] = parsed.verdict_date_ad

    parsed_meta = {
        "lifecycle_status": parsed.lifecycle_status,
        "verdict_type": new_verdict,
        "verdict_outcome_raw": parsed.verdict_outcome_raw,
        "unmapped": parsed.unmapped,
    }
    return updates, parsed_meta


def _hearings_of(case):
    return (case.extra_data or {}).get("enrichment_hearings") if case.extra_data else None


def run_backfill(session, batch_size=2000, limit=None, execute=False):
    """Iterate court_cases keyset-paginated, tally (and optionally apply) updates."""
    stats = {
        "scanned": 0,
        "header_cleared": 0,
        "verdict_type_set": 0,
        "verdict_date_set": 0,
        "meta_changed": 0,
        "rows_changed": 0,
    }
    last_key = None
    while True:
        q = session.query(CourtCase).order_by(
            CourtCase.court_identifier, CourtCase.case_number
        )
        if last_key is not None:
            court, number = last_key
            q = q.filter(
                or_(
                    CourtCase.court_identifier > court,
                    and_(
                        CourtCase.court_identifier == court,
                        CourtCase.case_number > number,
                    ),
                )
            )
        rows = q.limit(batch_size).all()
        if not rows:
            break

        for case in rows:
            stats["scanned"] += 1
            updates, meta = compute_case_updates(
                case.case_status,
                case.verdict_type,
                case.verdict_date_bs,
                case.verdict_date_ad,
                _hearings_of(case),
            )
            extra = case.extra_data or {}
            meta_changed = extra.get("parsed_status") != meta
            if not updates and not meta_changed:
                continue

            stats["rows_changed"] += 1
            if "case_status" in updates:
                stats["header_cleared"] += 1
            if "verdict_type" in updates:
                stats["verdict_type_set"] += 1
            if "verdict_date_bs" in updates:
                stats["verdict_date_set"] += 1
            if meta_changed:
                stats["meta_changed"] += 1

            if execute:
                for key, value in updates.items():
                    setattr(case, key, value)
                if extra is not case.extra_data:
                    case.extra_data = extra
                extra["parsed_status"] = meta
                flag_modified(case, "extra_data")

        if execute:
            session.commit()

        last_key = (rows[-1].court_identifier, rows[-1].case_number)
        logger.info("scanned=%(scanned)s changed=%(rows_changed)s", stats)
        if limit and stats["scanned"] >= limit:
            break

    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch-size", type=int, default=2000)
    ap.add_argument("--limit", type=int, default=None, help="stop after N rows (testing)")
    ap.add_argument("--execute", action="store_true", help="apply changes (default: dry-run)")
    ap.add_argument(
        "--i-understand-this-writes-prod",
        action="store_true",
        help="required second flag to actually write",
    )
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    write = args.execute and args.i_understand_this_writes_prod
    if args.execute and not write:
        raise SystemExit("Refusing to write: pass --i-understand-this-writes-prod too.")

    session = get_session(get_engine())
    try:
        stats = run_backfill(
            session, batch_size=args.batch_size, limit=args.limit, execute=write
        )
    finally:
        session.close()

    mode = "APPLIED" if write else "DRY-RUN (no writes)"
    print(f"\n=== backfill {mode} ===")
    for key, value in stats.items():
        print(f"  {key:16s}: {value:,}")


if __name__ == "__main__":
    main()
