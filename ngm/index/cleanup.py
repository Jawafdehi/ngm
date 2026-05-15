"""
NGM Index Cleanup — Keeps the 7 most recent successful index runs and
deletes everything older. Failed days (missing directories) are skipped
at listing time and never count against the 7-run quota.
"""

import logging
import os
from datetime import datetime

from cloudpathlib import AnyPath

from ngm.logging import setup

logger = logging.getLogger(__name__)

MAX_KEEP = 7


def _parse_date_from_dirname(dirname: str) -> datetime | None:
    """Parse a date string in YYYY-MM-DD format. Returns None on failure."""
    try:
        return datetime.strptime(dirname, "%Y-%m-%d")
    except ValueError:
        return None


def _list_date_dirs(indices_path: AnyPath) -> list[tuple[datetime, AnyPath]]:
    """List date-indexed directories sorted newest-first.

    Only directories whose name parses as YYYY-MM-DD are included.
    Non-date entries and non-directories are silently skipped.
    """
    if not indices_path.exists():
        return []

    dated = []
    for entry in indices_path.iterdir():
        if not entry.is_dir():
            continue
        date = _parse_date_from_dirname(entry.name)
        if date is None:
            continue
        dated.append((date, entry))

    dated.sort(key=lambda pair: pair[0], reverse=True)
    return dated


def _delete_old_indices(indices_path: AnyPath) -> int:
    """Delete all index directories beyond the MAX_KEEP most recent.

    Returns the number of directories deleted.
    """
    dated = _list_date_dirs(indices_path)

    if not dated:
        logger.info("No date-indexed directories found — nothing to clean")
        return 0

    keep = dated[:MAX_KEEP]
    delete = dated[MAX_KEEP:]

    if keep:
        oldest_kept = keep[-1][0].strftime("%Y-%m-%d")
        newest_kept = keep[0][0].strftime("%Y-%m-%d")
        logger.info(
            "Keeping %d most recent indices (%s — %s)",
            len(keep),
            oldest_kept,
            newest_kept,
        )

    if not delete:
        logger.info("No indices to delete (%d total, keeping up to %d)", len(dated), MAX_KEEP)
        return 0

    logger.info("Deleting %d old index director%s beyond the %d-run retention window",
                len(delete), "ies" if len(delete) != 1 else "y", MAX_KEEP)

    deleted = 0
    for _, entry in sorted(delete, key=lambda p: p[0]):
        logger.info("Deleting expired index directory: %s (date: %s)", entry.name, entry.name)
        try:
            entry.rmtree()
            deleted += 1
            logger.info("Deleted %s", entry.name)
        except OSError as e:
            logger.error("Failed to delete %s: %s", entry.name, e)

    return deleted


def main() -> None:
    """Main entry point for index cleanup."""
    files_store_env = os.getenv("FILES_STORE")
    if not files_store_env:
        logger.error("FILES_STORE environment variable must be set.")
        raise SystemExit(1)

    root_path = AnyPath(str(files_store_env))
    indices_path = root_path / "indices"

    now = datetime.now()
    logger.info("Index cleanup — %s", now.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("Indices path: %s", indices_path)
    logger.info("Retention policy: keep most recent %d successful index runs", MAX_KEEP)

    deleted = _delete_old_indices(indices_path)

    if deleted == 0:
        logger.info("Cleanup complete — nothing to delete")
    else:
        logger.info("Cleanup complete — deleted %d expired index director%s", deleted, "ies" if deleted != 1 else "y")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    setup()
    main()
