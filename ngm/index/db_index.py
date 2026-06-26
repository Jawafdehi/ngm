"""
Maintain the Postgres ``document_sources`` index — a live mirror of every
logical document in the NGM archive.

``collect_rows`` flattens a built index tree into row dicts; ``sync_document_sources``
upserts them and deletes rows that vanished. The mirror sweep is keyed off
``last_seen_build`` (one ``DELETE … WHERE last_seen_build != :build``) rather
than a giant ``NOT IN``, so it scales to 1M+ rows.
"""

import logging

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ngm.database.models import (
    DocumentSourceIndex,
    get_engine,
    get_session,
    init_db,
)

logger = logging.getLogger(__name__)

_UPSERT_BATCH = 1000

# Columns refreshed when a document_id already exists (everything except the PK,
# created_at, and updated_at — updated_at is set to now() on conflict below).
_UPDATE_COLS = (
    "dataset",
    "source_type",
    "title",
    "publication_date_bs",
    "primary_url",
    "html_url",
    "links",
    "doc_metadata",
    "index_path",
    "last_seen_build",
)


def _html_relpath(document_id: str) -> str:
    """Store-relative HTML landing-page key for a document_id (see build_index)."""
    segments = [s for s in document_id.split(":") if s]
    return "d/" + "/".join(segments) + ".html"


def collect_rows(root, base_url: str, build_id: str) -> list[dict]:
    """Flatten an index tree into ``document_sources`` row dicts.

    Each manuscript is tagged with its top-level dataset (the root child name).
    """
    base = base_url.rstrip("/")
    rows: list[dict] = []

    def walk(node, dataset: str) -> None:
        for ms in node.manuscripts:
            if not ms.document_id:
                continue
            meta = ms.metadata or {}
            rows.append(
                {
                    "document_id": ms.document_id,
                    "dataset": dataset,
                    "source_type": ms.source_type or "MISC",
                    "title": meta.get("title") or ms.file_name or None,
                    "publication_date_bs": (
                        meta.get("publication_date") or meta.get("date")
                    ),
                    "primary_url": ms.url or None,
                    "html_url": f"{base}/{_html_relpath(ms.document_id)}",
                    "links": ms.links,
                    "doc_metadata": meta,
                    "index_path": node.path,
                    "last_seen_build": build_id,
                }
            )
        for child in node.children:
            walk(child, dataset)

    for top in root.children:
        walk(top, top.name)
    return rows


def sync_document_sources(root, base_url: str, build_id: str, engine=None) -> dict:
    """Mirror the archive into ``document_sources``: upsert all, delete stale."""
    rows = collect_rows(root, base_url, build_id)
    if not rows:
        # An empty build must not wipe the table (treat as a no-op, not a mirror
        # of "nothing"). A real run always has documents.
        logger.warning("document_sources: 0 rows collected — skipping DB sync")
        return {"upserted": 0, "deleted": 0}

    engine = engine or get_engine()
    init_db(engine)  # create the table on first run (create_all is idempotent)
    session = get_session(engine)
    try:
        with session.begin():
            for i in range(0, len(rows), _UPSERT_BATCH):
                batch = rows[i : i + _UPSERT_BATCH]
                stmt = pg_insert(DocumentSourceIndex).values(batch)
                set_clause = {c: getattr(stmt.excluded, c) for c in _UPDATE_COLS}
                set_clause["updated_at"] = func.now()
                stmt = stmt.on_conflict_do_update(
                    index_elements=["document_id"], set_=set_clause
                )
                session.execute(stmt)

            deleted = session.execute(
                delete(DocumentSourceIndex).where(
                    DocumentSourceIndex.last_seen_build != build_id
                )
            ).rowcount

        logger.info(
            "document_sources: upserted %d, deleted %d stale", len(rows), deleted
        )
        return {"upserted": len(rows), "deleted": deleted}
    finally:
        session.close()
