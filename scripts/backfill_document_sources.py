#!/usr/bin/env python3
"""Backfill court_cases.document_sources from existing extra_data["court_orders"].

For every case that already has scraped order files but no document_sources yet,
build the DocumentSource list (same helper the pipeline uses) and store it. Pure
read-from-extra_data + write-to-new-column; no re-scrape, no network.

Idempotent and resumable: keyset-paginated over (court_identifier, case_number),
skipping rows that already have document_sources. Safe to re-run.

Run in the ngm image with DATABASE_URL set, e.g.:
    kubectl run ngm-backfill-docsources -n app --rm -it --restart=Never \
      --image=jawafdehi/ngm:main --overrides='{"spec":{"containers":[{"name":"x",
      "image":"jawafdehi/ngm:main","command":["python","scripts/backfill_document_sources.py"],
      "envFrom":[{"secretRef":{"name":"ngm-env"}}]}]}}'
"""

import os
import sys

from sqlalchemy import tuple_

from ngm.database.models import CourtCase, get_engine, get_session
from ngm.utils.document_sources import order_document_sources

BATCH = 1000


def main():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("DATABASE_URL must be set")
    base_url = os.getenv("NGM_STORE_BASE_URL")  # None -> helper default

    engine = get_engine(db_url)
    session = get_session(engine)

    last = ("", "")
    scanned = updated = skipped = empty = 0
    try:
        while True:
            with session.begin():
                batch = (
                    session.query(CourtCase)
                    .filter(
                        CourtCase.extra_data["court_orders"].isnot(None),
                        tuple_(CourtCase.court_identifier, CourtCase.case_number)
                        > last,
                    )
                    .order_by(CourtCase.court_identifier, CourtCase.case_number)
                    .limit(BATCH)
                    .all()
                )
                if not batch:
                    break
                for c in batch:
                    scanned += 1
                    if c.document_sources is not None:
                        skipped += 1
                        continue
                    paths = (c.extra_data or {}).get("court_orders")
                    if not isinstance(paths, list):
                        paths = []
                    ds = order_document_sources(
                        c.court_identifier, c.case_number, paths, base_url=base_url
                    )
                    if ds:
                        c.document_sources = ds
                        updated += 1
                    else:
                        # has court_orders but no usable paths — store [] so this
                        # row is not re-scanned forever (keyset already prevents
                        # loops, but [] keeps the semantics unambiguous).
                        c.document_sources = []
                        empty += 1
                last = (batch[-1].court_identifier, batch[-1].case_number)
            print(
                f"...scanned={scanned} updated={updated} skipped={skipped} empty={empty}",
                flush=True,
            )
    finally:
        session.close()
        engine.dispose()

    print(
        f"DONE scanned={scanned} updated={updated} skipped={skipped} empty={empty}",
        flush=True,
    )


if __name__ == "__main__":
    main()
