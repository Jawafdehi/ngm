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
from datetime import datetime
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
    }

    def __init__(self, limit=5, *args, **kwargs):
        """
        Initialize spider with database connection.

        Args:
            limit: Maximum number of cases to enrich (default: 5)
        """
        super().__init__(*args, **kwargs)

        self.limit = int(limit)
        self.engine = get_engine()
        init_db(self.engine)
        self.session = get_session(self.engine)

        self.logger.info(f"Enrichment spider initialized (limit: {self.limit})")

    def start(self):
        return super().start()

    def start_requests(self):
        """
        Query pending cases, reserve them, then yield download requests.

        Reservation pattern: mark cases as in_progress before yielding
        requests so concurrent spider runs don't process the same cases.
        """
        with self.session.begin():
            cases_to_enrich = (
                self.session.query(CourtCase)
                .filter(
                    and_(
                        CourtCase.extra_data["order_document_url"].astext.isnot(None),
                        or_(
                            CourtCase.extra_data["orders_scraped"].astext.is_(None),
                            CourtCase.extra_data["orders_scraped"].astext != "true",
                        ),
                        or_(
                            CourtCase.extra_data["order_in_progress"].astext.is_(None),
                            CourtCase.extra_data["order_in_progress"].astext != "true",
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

            # Reserve all cases in the same transaction before yielding requests.
            # This prevents race conditions if two spiders run simultaneously.
            for case in cases_to_enrich:
                if case.extra_data is None:
                    case.extra_data = {}
                case.extra_data["order_in_progress"] = True
                case.extra_data["order_started_at"] = (
                    datetime.now(KATHMANDU_TZ).replace(tzinfo=None).isoformat()
                )
                flag_modified(case, "extra_data")

            # Context manager auto-commits reservation here on exit

        # Yield requests AFTER reservation is committed to DB
        for case in cases_to_enrich:
            document_url = case.extra_data.get("order_document_url")

            if not document_url:
                self.logger.warning(f"Case {case.case_number} has no document URL")
                continue

            url_path = urlparse(document_url).path
            file_ext = os.path.splitext(url_path)[1]
            if not file_ext:
                file_ext = ".doc"

            self.logger.info(
                f"Queuing download for case {case.case_number}: {document_url}"
            )

            yield scrapy.Request(
                url=document_url,
                callback=self.parse_document,
                meta={
                    "case_number": case.case_number,
                    "court_identifier": case.court_identifier,
                    "document_url": document_url,
                    "file_extension": file_ext,
                },
                dont_filter=True,
                errback=self.handle_error,
            )

    def handle_error(self, failure):
        """Handle network/download errors from Scrapy."""
        request = failure.request
        case_number = request.meta.get("case_number")

        self.logger.error(
            f"Error downloading document for case {case_number}: {failure.value}"
        )

        try:
            with self.session.begin():
                case = (
                    self.session.query(CourtCase)
                    .filter_by(
                        case_number=case_number,
                        court_identifier=request.meta.get("court_identifier"),
                    )
                    .first()
                )

                if case:
                    if case.extra_data is None:
                        case.extra_data = {}

                    case.extra_data["orders_failed"] = True
                    case.extra_data["orders_error"] = str(failure.value)
                    case.extra_data["orders_failed_at"] = (
                        datetime.now(KATHMANDU_TZ).replace(tzinfo=None).isoformat()
                    )
                    case.extra_data.pop("order_in_progress", None)
                    case.extra_data.pop("order_started_at", None)
                    flag_modified(case, "extra_data")

        except Exception as e:
            self.logger.error(f"Error updating database for failed case: {e}")

    def parse_document(self, response):
        """
        Write downloaded document directly to disk.

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

        case_number_safe = case_number.replace("/", "-")
        file_dir = os.path.join(
            FILES_STORE, "court", "orders", court_identifier, case_number_safe
        )

        # TODO(GitHub #XXX): Support multiple files per case (file_2, file_3, etc.)
        filename = f"file_1{file_extension}"
        file_path = os.path.join(file_dir, filename)

        try:
            os.makedirs(file_dir, exist_ok=True)

            with open(file_path, "wb") as f:
                f.write(response.body)

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
            with open(metadata_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)

            relative_path = os.path.join(
                "court", "orders", court_identifier, case_number_safe, filename
            )
            self._mark_download_success(case_number, court_identifier, relative_path)

        except Exception as e:
            self.logger.error(f"Error saving document for case {case_number}: {e}")
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
                    case.status = "enriched"
                    case.extra_data.pop("order_in_progress", None)
                    case.extra_data.pop("order_started_at", None)
                    flag_modified(case, "extra_data")

                    self.logger.info(
                        f"Updated database: {case_number} - orders_scraped=true"
                    )

        except Exception as e:
            self.logger.error(f"Error updating database for successful download: {e}")

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
                    case.extra_data.pop("order_in_progress", None)
                    case.extra_data.pop("order_started_at", None)
                    
                    # Set case status to failed to match other enrichment spiders
                    case.status = "failed"
                    
                    flag_modified(case, "extra_data")

                    self.logger.info(
                        f"Updated database: {case_number} - orders_failed=true, status=failed"
                    )

        except Exception as e:
            self.logger.error(f"Error updating database for failed download: {e}")

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

        except Exception as e:
            self.logger.error(f"Error in cleanup: {e}")
