"""
Supreme Court Orders Enrichment Spider

Downloads order documents for cases that have been identified by supreme_court_orders.py.
This spider reads cases with order_document_url in extra_data that haven't been
downloaded yet, then downloads the actual order documents directly.

No FilesPipeline - handles file writing directly to avoid double-downloading.
"""

import scrapy
import os
import json
import re
import tempfile
from datetime import datetime, timedelta
import pytz
from sqlalchemy import and_, or_
from sqlalchemy.orm.attributes import flag_modified
from urllib.parse import urlparse

from ngm.database.models import get_engine, get_session, init_db, CourtCase
from ngm.ngscrape.settings import FILES_STORE

KATHMANDU_TZ = pytz.timezone("Asia/Kathmandu")


class SupremeOrdersEnrichmentSpider(scrapy.Spider):
    name = "supreme_orders_enrichment"

    custom_settings = {
        # No FilesPipeline - we handle file writing directly
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 408, 429],
        "CONCURRENT_REQUESTS": 1,
        "DOWNLOAD_DELAY": 3,
        "DOWNLOAD_TIMEOUT": 60,
        "USER_AGENT": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "DEFAULT_REQUEST_HEADERS": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ne;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        },
    }

    def __init__(self, limit=5, *args, **kwargs):
        """
        Initialize spider with database connection.

        Args:
            limit: Maximum number of cases to enrich (default: 5)
        """
        super().__init__(*args, **kwargs)

        try:
            self.limit = int(limit)
        except (ValueError, TypeError) as e:
            raise ValueError(f"limit must be a valid integer, got: {limit}") from e

        if self.limit <= 0:
            raise ValueError(f"limit must be positive, got: {self.limit}")

        self.engine = get_engine()
        init_db(self.engine)
        self.session = get_session(self.engine)

        self.logger.info(f"Enrichment spider initialized (limit: {self.limit})")

    def start_requests(self):
        """
        Query pending cases, reserve them, then yield download requests.

        Reservation pattern: mark cases as in_progress before yielding
        requests so concurrent spider runs don't process the same cases.
        Stale lease recovery: reclaim cases where order_started_at is older
        than 30 minutes (crashed workers).
        """
        # Calculate stale threshold (30 minutes ago)
        stale_threshold = (
            (datetime.now(KATHMANDU_TZ) - timedelta(minutes=30))
            .replace(tzinfo=None)
            .isoformat()
        )

        with self.session.begin():
            cases_to_enrich = (
                self.session.query(CourtCase)
                .filter(
                    and_(
                        CourtCase.extra_data["order_document_url"].astext.isnot(None),
                        CourtCase.extra_data["order_document_url"].astext != "",
                        or_(
                            CourtCase.extra_data["orders_scraped"].astext.is_(None),
                            CourtCase.extra_data["orders_scraped"].astext != "true",
                        ),
                        or_(
                            # Not in progress
                            CourtCase.extra_data["order_in_progress"].astext.is_(None),
                            CourtCase.extra_data["order_in_progress"].astext != "true",
                            # Or stale (started more than 30 minutes ago)
                            CourtCase.extra_data["order_started_at"].astext
                            < stale_threshold,
                        ),
                    )
                )
                .order_by(CourtCase.registration_date_ad.desc().nullslast())
                .limit(self.limit)
                .with_for_update(skip_locked=True)  # Lock rows, skip already locked
                .all()
            )

            if not cases_to_enrich:
                self.logger.info("No cases to enrich")
                return

            self.logger.info(f"Found {len(cases_to_enrich)} cases to enrich")

            # Materialize data into plain dicts INSIDE transaction to avoid ORM expiration
            request_payloads = []
            for case in cases_to_enrich:
                if case.extra_data is None:
                    case.extra_data = {}
                case.extra_data["order_in_progress"] = True
                case.extra_data["order_started_at"] = (
                    datetime.now(KATHMANDU_TZ).replace(tzinfo=None).isoformat()
                )
                flag_modified(case, "extra_data")

                # Extract data while ORM objects are still attached
                document_url = case.extra_data.get("order_document_url")
                if document_url:
                    url_path = urlparse(document_url).path
                    file_ext = os.path.splitext(url_path)[1]
                    if not file_ext:
                        file_ext = ".doc"

                    request_payloads.append(
                        {
                            "case_number": case.case_number,
                            "court_identifier": case.court_identifier,
                            "document_url": document_url,
                            "file_extension": file_ext,
                        }
                    )

            # Transaction commits here on exit

        # Yield requests AFTER transaction using materialized data
        for payload in request_payloads:
            self.logger.info(
                f"Queuing download for case {payload['case_number']}: {payload['document_url']}"
            )

            yield scrapy.Request(
                url=payload["document_url"],
                callback=self.parse_document,
                meta=payload,
                dont_filter=True,
                errback=self.handle_error,
            )

    def handle_error(self, failure):
        """Handle network/download errors from Scrapy."""
        request = failure.request
        case_number = request.meta.get("case_number")
        court_identifier = request.meta.get("court_identifier")

        self.logger.error(
            f"Error downloading document for case {case_number}: {failure.value}"
        )

        # Use centralized failure handler
        self._mark_download_failed(case_number, court_identifier, str(failure.value))

    def parse_document(self, response):
        """
        Write downloaded document directly to disk using atomic writes.

        Scrapy has already fetched the file into response.body.
        We write it directly — no FilesPipeline, no second download.
        """
        case_number = response.meta["case_number"]
        court_identifier = response.meta["court_identifier"]
        document_url = response.meta["document_url"]
        file_extension = response.meta["file_extension"]

        if response.status != 200:
            self.logger.error(f"HTTP {response.status} for case {case_number}")
            self._mark_download_failed(
                case_number, court_identifier, f"HTTP {response.status}"
            )
            return

        # CHECK if FILES_STORE is a remote path
        if FILES_STORE.startswith(("s3://", "gs://", "ftp://")):
            self.logger.error(
                f"Enrichment spider requires local filesystem storage. "
                f"Got remote storage: {FILES_STORE}. "
                f"Use a storage backend pipeline for remote storage."
            )
            self._mark_download_failed(
                case_number,
                court_identifier,
                f"Remote storage not supported: {FILES_STORE}",
            )
            return

        self.logger.info(
            f"Downloaded document for case {case_number} ({len(response.body)} bytes)"
        )

        # Sanitize case_number and court_identifier for safe filenames
        case_number_safe = re.sub(r"[^A-Za-z0-9._-]+", "_", case_number).strip("._-")
        if not case_number_safe:
            case_number_safe = "unknown_case"

        court_identifier_safe = re.sub(
            r"[^A-Za-z0-9._-]+", "_", str(court_identifier)
        ).strip("._-")
        if not court_identifier_safe:
            court_identifier_safe = "unknown_court"

        # Sanitize file extension
        file_extension_safe = re.sub(r"[^A-Za-z0-9._-]+", "_", file_extension).strip(
            "._-"
        )
        if not file_extension_safe.startswith("."):
            file_extension_safe = f".{file_extension_safe}"

        file_dir = os.path.join(
            FILES_STORE, "court", "orders", court_identifier_safe, case_number_safe
        )

        # Single file per case for now; multi-file support can be added if needed
        filename = f"file_1{file_extension_safe}"
        file_path = os.path.join(file_dir, filename)

        try:
            os.makedirs(file_dir, exist_ok=True)

            # Atomic write: write to temp file, then rename
            temp_fd, temp_path = tempfile.mkstemp(dir=file_dir, suffix=".tmp")
            try:
                with os.fdopen(temp_fd, "wb") as f:
                    f.write(response.body)
                    f.flush()
                    os.fsync(f.fileno())

                # Atomically replace temp file with final file
                os.replace(temp_path, file_path)
            except Exception:
                # Clean up temp file on error
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise

            self.logger.info(f"Saved document to: {file_path}")

            metadata = {
                "case_number": case_number,
                "court_identifier": court_identifier,
                "document_url": document_url,
                "scraped_at": datetime.now(KATHMANDU_TZ)
                .replace(tzinfo=None)
                .isoformat(),
                "file_size": len(response.body),
            }

            metadata_path = os.path.join(file_dir, "metadata.json")
            metadata_temp_path = f"{metadata_path}.tmp"

            try:
                with open(metadata_temp_path, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())

                # Atomically replace temp metadata with final metadata
                os.replace(metadata_temp_path, metadata_path)
            except Exception:
                # Clean up temp metadata on error
                if os.path.exists(metadata_temp_path):
                    os.unlink(metadata_temp_path)
                raise

            relative_path = os.path.join(
                "court", "orders", court_identifier_safe, case_number_safe, filename
            )
            self._mark_download_success(case_number, court_identifier, relative_path)

        except Exception as e:
            self.logger.exception(f"Error saving document for case {case_number}")
            self._mark_download_failed(case_number, court_identifier, str(e))

    def _mark_download_success(self, case_number, court_identifier, file_path):
        """Mark case as successfully downloaded in database."""
        try:
            with self.session.begin():
                case = (
                    self.session.query(CourtCase)
                    .filter_by(
                        case_number=case_number, court_identifier=court_identifier
                    )
                    .first()
                )

                if case:
                    if case.extra_data is None:
                        case.extra_data = {}

                    case.extra_data["orders_scraped"] = True
                    case.extra_data["orders_scraped_at"] = (
                        datetime.now(KATHMANDU_TZ).replace(tzinfo=None).isoformat()
                    )
                    case.extra_data["orders_file_path"] = file_path

                    # Clear any previous failure state
                    case.extra_data.pop("orders_failed", None)
                    case.extra_data.pop("orders_error", None)
                    case.extra_data.pop("orders_failed_at", None)

                    # Don't set case.status - it tracks case detail scrape status
                    case.extra_data.pop("order_in_progress", None)
                    case.extra_data.pop("order_started_at", None)
                    flag_modified(case, "extra_data")

                    self.logger.info(
                        f"Updated database: {case_number} - orders_scraped=true"
                    )

        except Exception:
            self.logger.exception("Error updating database for successful download")

    def _mark_download_failed(self, case_number, court_identifier, error_message):
        """Mark case as failed download in database."""
        try:
            with self.session.begin():
                case = (
                    self.session.query(CourtCase)
                    .filter_by(
                        case_number=case_number, court_identifier=court_identifier
                    )
                    .first()
                )

                if case:
                    if case.extra_data is None:
                        case.extra_data = {}

                    case.extra_data["orders_failed"] = True
                    case.extra_data["orders_error"] = error_message
                    case.extra_data["orders_failed_at"] = (
                        datetime.now(KATHMANDU_TZ).replace(tzinfo=None).isoformat()
                    )

                    # Clear any previous success state
                    case.extra_data.pop("orders_scraped", None)
                    case.extra_data.pop("orders_scraped_at", None)
                    case.extra_data.pop("orders_file_path", None)

                    case.extra_data.pop("order_in_progress", None)
                    case.extra_data.pop("order_started_at", None)

                    # Don't set case.status - it tracks case detail scrape status
                    flag_modified(case, "extra_data")

                    self.logger.info(
                        f"Updated database: {case_number} - orders_failed=true"
                    )

        except Exception:
            self.logger.exception("Error updating database for failed download")

    def closed(self, reason):
        """Spider cleanup - close session and dispose engine."""
        try:
            if hasattr(self, "session") and self.session:
                self.session.close()
                self.logger.info("Database session closed")

            if hasattr(self, "engine") and self.engine:
                self.engine.dispose()
                self.logger.info("Database engine disposed")

            self.logger.info(f"Enrichment spider closed: {reason}")

        except Exception:
            self.logger.exception("Error in cleanup")
