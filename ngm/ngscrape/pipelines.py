import json
import os
import uuid
from datetime import datetime
from urllib.parse import urlparse
import pytz
from scrapy.pipelines.files import FilesPipeline
from sqlalchemy.orm.attributes import flag_modified
from ngm.database.models import CourtCase

KATHMANDU_TZ = pytz.timezone("Asia/Kathmandu")

# Retry limit for cases with no document_url download links for Supreme_court_orders spider
RETRY_LIMIT_NO_DOCS = 3


class KanunPatrikaPipeline(FilesPipeline):
    """Pipeline for downloading Kanun Patrika PDF files with custom naming."""

    def file_path(self, request, response=None, info=None, *, item=None):
        """Generate custom file path based on metadata."""
        metadata = item.get("metadata", {})
        file_id = request.url.split("/")[-1].replace(".pdf", "")

        if metadata:
            year = metadata.get("year", "")
            month = metadata.get("month", "")
            volume = metadata.get("volume", "")
            issue = metadata.get("issue", "")
            return f"{year} {month} भाग {volume} अंक {issue} - {file_id}.pdf"

        return f"{file_id}.pdf"

    def item_completed(self, results, item, info):
        for ok, result in results:
            if ok:
                file_path = result["path"]
                info.spider.logger.info(f"Downloaded: {file_path}")
            else:
                info.spider.logger.error(f"Failed: {item['file_urls'][0]}")
        return item


class CiaaAnnualReportsPipeline(FilesPipeline):
    """Pipeline for downloading CIAA Annual Reports PDF files with metadata."""

    def file_path(self, request, response=None, info=None, *, item=None):
        """Generate custom file path based on metadata."""
        metadata = item.get("metadata", {})
        file_id = request.url.split("/")[-1].replace(".pdf", "")

        if metadata:
            serial_number = metadata.get("serial_number", "")
            title = metadata.get("title", "").replace("/", "-")
            # Clean title for filename
            safe_title = "".join(
                c for c in title if c.isalnum() or c in (" ", "-", "_")
            ).strip()
            if safe_title:
                return f"{serial_number}. {safe_title} - {file_id}.pdf"

        return f"{file_id}.pdf"

    def item_completed(self, results, item, info):
        """Save simplified metadata and log download results."""
        metadata = item.get("metadata", {})
        files_store = info.spider.settings.get("FILES_STORE")
        file_path = None

        for ok, result in results:
            if ok:
                file_path = result["path"]

        if metadata and file_path:
            simple_meta = {
                "serial_number": metadata.get("serial_number", ""),
                "date": metadata.get("date", ""),
                "title": metadata.get("title", ""),
                "file_name": os.path.basename(file_path),
            }

            metadata_path = os.path.join(
                files_store, file_path.replace(".pdf", ".json")
            )
            os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
            with open(metadata_path, "w", encoding="utf-8") as f:
                json.dump(simple_meta, f, ensure_ascii=False, indent=2)

            info.spider.logger.info(f"Saved metadata: {metadata_path}")

        return item


class SupremeCourtOrdersPipeline(FilesPipeline):
    """
    Pipeline for Supreme Court order documents.

    Responsibilities:
    - Downloads files using Scrapy's FilesPipeline (storage backend configured via FILES_STORE setting)
    - Generates organized file paths: court-orders/{court}/{case}.{n}.{ext}
    - Updates database with file paths in extra_data["court_orders"]
    - Tracks success/failure to skip already-scraped cases on next run
    """

    def open_spider(self, spider):
        """Initialize pipeline with database session from spider."""
        super().open_spider(spider)

        if not hasattr(spider, "session"):
            raise RuntimeError(
                f"SupremeCourtOrdersPipeline requires spider to have a database session. "
                f"Spider '{spider.name}' has no 'session' attribute."
            )

        self.session = spider.session

    def file_path(self, request, response=None, info=None, *, item=None):
        """
        Generate file path for each document.

        Format: court-orders/{court_identifier}/{case_number}.{n}.{ext}
        Example: court-orders/special/082-OA-0503.1.docx

        Args:
            request: Scrapy request object containing the download URL
            item: Item containing case_number and court_identifier

        Returns:
            str: File path relative to FILES_STORE
        """
        court_identifier = item.get("court_identifier")
        case_number = item.get("case_number")

        if not court_identifier or not case_number:
            raise ValueError(
                f"Item missing required fields. "
                f"court_identifier: {court_identifier!r}, "
                f"case_number: {case_number!r}"
            )

        # Make case number filesystem-safe (replace slashes with dashes)
        case_number_safe = case_number.replace("/", "-")

        # Number files sequentially (.1, .2, .3) for cases with multiple documents
        all_urls = item.get("file_urls", [])
        if request.url not in all_urls:
            # Log error but don't crash - return a fallback path
            court_identifier = item.get("court_identifier", "unknown")
            case_number_safe = item.get("case_number", "unknown").replace("/", "-")
            info.spider.logger.error(
                f"Request URL {request.url!r} not found in item file_urls. "
                f"Available URLs: {all_urls}. Using fallback path."
            )
            # Use UUID to avoid collisions in fallback paths
            unique_id = uuid.uuid4().hex[:8]
            ext = os.path.splitext(urlparse(request.url).path)[1] or ".doc"
            return f"court-orders/{court_identifier}/{case_number_safe}.error-{unique_id}{ext}"

        n = all_urls.index(request.url) + 1

        # Extract file extension from download URL, fallback to .doc
        ext = os.path.splitext(urlparse(request.url).path)[1] or ".doc"

        return f"court-orders/{court_identifier}/{case_number_safe}.{n}{ext}"

    def item_completed(self, results, item, info):
        """
        Process completed downloads and update database.

        Called by Scrapy after all files in the item have been processed.

        Handles two types of items:
        1. Success items: file_urls contains download URLs
        2. Error items: file_urls is empty, error field contains error message

        Args:
            results: List of (success, result_dict) tuples from FilesPipeline
            item: The scraped item
            info: Spider info object

        Returns:
            item: The original item (unchanged)
        """
        case_number = item.get("case_number")
        court_identifier = item.get("court_identifier")

        if not case_number or not court_identifier:
            raise ValueError(
                f"item_completed called with missing required fields. "
                f"case_number: {case_number!r}, "
                f"court_identifier: {court_identifier!r}"
            )

        # Check if spider sent an error item (parsing/scraping failure)
        spider_error = item.get("error")

        successful_paths = []
        failed_results = []

        for ok, result in results:
            if ok:
                # result["path"] is the file path from file_path()
                # e.g., "court-orders/special/082-OA-0503.1.docx"
                file_path = result["path"]
                successful_paths.append(file_path)
                info.spider.logger.info(f"[{case_number}] Saved: {file_path}")
            else:
                # File download/upload failed
                failed_results.append(str(result))
                info.spider.logger.error(f"[{case_number}] Failed: {result}")

        # Update database
        if successful_paths:
            self._mark_success(
                info.spider, case_number, court_identifier, successful_paths
            )
            info.spider.successful_cases += 1
        else:
            # Check if this is a "no download links" error (temporary failure)
            if spider_error == "no_download_links":
                # Increment retry counter instead of marking as failed
                self._increment_no_docs_count(
                    info.spider, case_number, court_identifier
                )
                # Don't increment failed_cases counter - this is a retry
            else:
                # Permanent failure - mark as failed
                error = spider_error or "; ".join(failed_results) or "Unknown failure"
                self._mark_failed(info.spider, case_number, court_identifier, error)
                info.spider.failed_cases += 1

        return item

    def _mark_success(self, spider, case_number, court_identifier, file_paths):
        """
        Mark case as successfully scraped in database.

        Updates extra_data with:
        - court_orders: List of file paths
        - court_orders_scraped_at: ISO timestamp
        - Clears any previous failure state

        Args:
            spider: Spider instance
            case_number: Case number (e.g., "082-OA-0503")
            court_identifier: Court identifier (e.g., "special")
            file_paths: List of file paths (e.g., ["court-orders/special/082-OA-0503.1.docx"])
        """
        try:
            with self.session.begin():
                case = (
                    self.session.query(CourtCase)
                    .filter_by(
                        case_number=case_number, court_identifier=court_identifier
                    )
                    .first()
                )

                if not case:
                    spider.logger.error(
                        f"[{case_number}] Case not found in database - may have been deleted. "
                        f"Files saved but database not updated."
                    )
                    return

                # Initialize extra_data if needed
                if case.extra_data is None:
                    case.extra_data = {}

                # Store file paths and timestamp
                case.extra_data["court_orders"] = file_paths
                case.extra_data["court_orders_scraped_at"] = (
                    datetime.now(KATHMANDU_TZ).replace(tzinfo=None).isoformat()
                )

                # Clear any previous failure state
                case.extra_data.pop("orders_failed", None)
                case.extra_data.pop("orders_error", None)
                case.extra_data.pop("orders_failed_at", None)

                # Clear retry counter on success
                case.extra_data.pop("orders_no_docs_count", None)
                case.extra_data.pop("orders_no_docs_last_tried", None)

                # Mark field as modified for SQLAlchemy JSONB tracking
                flag_modified(case, "extra_data")
        except Exception as e:
            spider.logger.exception(
                f"[{case_number}] Unexpected error marking case as successful: {e}"
            )

    def _mark_failed(self, spider, case_number, court_identifier, error):
        """
        Mark case as failed in database.

        Updates extra_data with:
        - orders_failed: true
        - orders_error: Error message
        - orders_failed_at: ISO timestamp

        Failed cases are skipped on next spider run (filtered in _get_cases_to_scrape).

        Args:
            spider: Spider instance
            case_number: Case number (e.g., "082-OA-0503")
            court_identifier: Court identifier (e.g., "special")
            error: Error message string
        """
        try:
            with self.session.begin():
                case = (
                    self.session.query(CourtCase)
                    .filter_by(
                        case_number=case_number, court_identifier=court_identifier
                    )
                    .first()
                )

                if not case:
                    spider.logger.error(
                        f"[{case_number}] Case not found in database - may have been deleted. "
                        f"Cannot mark as failed."
                    )
                    return

                # Initialize extra_data if needed
                if case.extra_data is None:
                    case.extra_data = {}

                # Store failure state
                case.extra_data["orders_failed"] = True
                case.extra_data["orders_error"] = error
                case.extra_data["orders_failed_at"] = (
                    datetime.now(KATHMANDU_TZ).replace(tzinfo=None).isoformat()
                )

                # Mark field as modified for SQLAlchemy JSONB tracking
                flag_modified(case, "extra_data")
        except Exception as e:
            spider.logger.exception(
                f"[{case_number}] Unexpected error marking case as failed: {e}"
            )

    def _increment_no_docs_count(self, spider, case_number, court_identifier):
        """
        Increment retry counter for cases with no download links.

        This is used for temporary failures where documents might be added later.
        Cases with count > RETRY_LIMIT_NO_DOCS are filtered out in spider's _get_cases_to_scrape.
        When count reaches RETRY_LIMIT_NO_DOCS, the case is marked as permanently failed.

        Updates extra_data with:
        - orders_no_docs_count: Incremented counter
        - orders_no_docs_last_tried: ISO timestamp
        - If count reaches RETRY_LIMIT_NO_DOCS: marks as failed (orders_failed = true)

        Args:
            spider: Spider instance
            case_number: Case number (e.g., "082-OA-0503")
            court_identifier: Court identifier (e.g., "special")
        """
        try:
            with self.session.begin():
                case = (
                    self.session.query(CourtCase)
                    .filter_by(
                        case_number=case_number, court_identifier=court_identifier
                    )
                    .first()
                )

                if not case:
                    spider.logger.error(
                        f"[{case_number}] Case not found in database - may have been deleted. "
                        f"Cannot increment no_docs counter."
                    )
                    return

                # Initialize extra_data if needed
                if case.extra_data is None:
                    case.extra_data = {}

                # Read existing count and increment
                existing_count = case.extra_data.get("orders_no_docs_count", 0)
                new_count = existing_count + 1

                # Update counter and timestamp
                case.extra_data["orders_no_docs_count"] = new_count
                case.extra_data["orders_no_docs_last_tried"] = (
                    datetime.now(KATHMANDU_TZ).replace(tzinfo=None).isoformat()
                )

                # If we've hit the retry limit, mark as permanently failed
                if new_count >= RETRY_LIMIT_NO_DOCS:
                    case.extra_data["orders_failed"] = True
                    case.extra_data["orders_error"] = (
                        f"No download links found after {new_count} attempts"
                    )
                    case.extra_data["orders_failed_at"] = (
                        datetime.now(KATHMANDU_TZ).replace(tzinfo=None).isoformat()
                    )
                    spider.logger.warning(
                        f"[{case_number}] Reached retry limit ({new_count}/{RETRY_LIMIT_NO_DOCS}). "
                        "Marking as permanently failed."
                    )
                else:
                    spider.logger.info(
                        f"[{case_number}] No download links retry count: {new_count}/{RETRY_LIMIT_NO_DOCS}"
                    )

                # Mark field as modified for SQLAlchemy JSONB tracking
                flag_modified(case, "extra_data")
        except Exception as e:
            spider.logger.exception(
                f"[{case_number}] Unexpected error incrementing no_docs counter: {e}"
            )
