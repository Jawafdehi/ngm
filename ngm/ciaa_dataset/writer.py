"""Writer - writes CIAACase records and indexes as JSON (local or S3/R2)."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
from datetime import datetime, timezone

from cloudpathlib import AnyPath, S3Client

from ngm.ciaa_dataset.models import CIAACase

logger = logging.getLogger(__name__)

# Switch between local and S3/R2 by setting OUTPUT_DIR environment variable:
# For local: OUTPUT_DIR="output/ciaa_dataset"
# For R2: OUTPUT_DIR="s3://ngm-testing/ciaa_dataset"

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output/ciaa_dataset")

# Configure S3Client for Cloudflare R2 if using S3
if OUTPUT_DIR.startswith("s3://") and os.getenv("AWS_ENDPOINT_URL"):
    S3Client(
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        endpoint_url=os.getenv("AWS_ENDPOINT_URL"),
    ).set_as_default_client()

# Parallel S3 write workers
_MAX_WRITE_WORKERS = 10


class FileWriter:
    """Writes CIAACase records and indexes as JSON files (local or S3)."""

    def __init__(self, output_dir: str = OUTPUT_DIR):
        self.root = AnyPath(output_dir)
        self._pending_writes: list[tuple[AnyPath, str]] = []
        logger.info("FileWriter: writing to %s", self.root)

    def write_case(self, case: CIAACase) -> None:
        """Queue a case for writing (actual write happens in flush())."""
        safe_no = case.case_no.replace("/", "-")
        path = self.root / f"ciaa/cases/{case.fiscal_year}/{safe_no}.json"

        case_dict = case.model_dump(by_alias=True)
        content = json.dumps(case_dict, ensure_ascii=False, indent=2)

        self._pending_writes.append((path, content))

    def flush(self) -> None:
        """Write all pending cases in parallel."""
        if not self._pending_writes:
            return

        total = len(self._pending_writes)
        logger.info(
            "Writing %d cases in parallel (%d workers)...", total, _MAX_WRITE_WORKERS
        )

        completed = 0
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=_MAX_WRITE_WORKERS
        ) as executor:
            futures = {
                executor.submit(self._write_file, path, content): path
                for path, content in self._pending_writes
            }

            for future in concurrent.futures.as_completed(futures):
                path = futures[future]
                try:
                    future.result()
                    completed += 1
                    # Log progress every 10 cases
                    if completed % 10 == 0 or completed == total:
                        logger.info("  [%d/%d] cases written", completed, total)
                except Exception as e:
                    logger.error("Failed to write %s: %s", path, e)
                    raise

        logger.info("Wrote %d cases", total)
        self._pending_writes.clear()

    def _write_file(self, path: AnyPath, content: str) -> None:
        """Write a single file (called from thread pool)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def write_fiscal_year_index(
        self, fiscal_year: str, cases: list[CIAACase], stats: dict
    ) -> None:
        """Write the fiscal year index JSON."""
        index = {
            "fiscal_year": fiscal_year,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stats": {
                "total": stats.get("total", 0),
                "matched": stats.get("matched", 0),
                "needs_review": stats.get("needs_review", 0),
                "unmatched": stats.get("unmatched", 0),
            },
            "cases": [
                {
                    "case_no": c.case_no,
                    "case_title": c.case_title,
                    "match_status": c.meta.match_status,
                }
                for c in cases
            ],
        }

        path = self.root / f"ciaa/cases/{fiscal_year}/index.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        logger.info("Wrote FY index for %s (%d cases)", fiscal_year, len(cases))
