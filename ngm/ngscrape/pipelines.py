import json
import os
from datetime import datetime
from urllib.parse import urlparse
import pytz
from scrapy.pipelines.files import FilesPipeline
from sqlalchemy.orm.attributes import flag_modified
from ngm.database.models import CourtCase

KATHMANDU_TZ = pytz.timezone("Asia/Kathmandu")


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

    Storage:
    - FILES_STORE setting determines storage backend (S3, local filesystem, etc.)
    - File paths are relative to FILES_STORE root

    Database schema (extra_data JSONB column):
        Success: {
            "court_orders": ["court-orders/special/082-OA-0503.1.docx", ...],
            "court_orders_scraped_at": "2026-03-03T12:48:58"
        }
        Failure: {
            "orders_failed": true,
            "orders_error": "Results table found but no download links",
            "orders_failed_at": "2026-03-03T12:49:07"
        }
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
            raise ValueError(
                f"Request URL {request.url!r} not found in item file_urls. "
                f"Available URLs: {all_urls}"
            )
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

        # Process download results
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
            # Use spider error if provided, otherwise file upload error
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
        with self.session.begin():
            case = (
                self.session.query(CourtCase)
                .filter_by(case_number=case_number, court_identifier=court_identifier)
                .first()
            )

            if not case:
                raise LookupError(
                    f"Case not found in database. "
                    f"case_number={case_number!r}, "
                    f"court_identifier={court_identifier!r}"
                )

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

            # Mark field as modified for SQLAlchemy JSONB tracking
            flag_modified(case, "extra_data")

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
        with self.session.begin():
            case = (
                self.session.query(CourtCase)
                .filter_by(case_number=case_number, court_identifier=court_identifier)
                .first()
            )

            if not case:
                raise LookupError(
                    f"Case not found in database. "
                    f"case_number={case_number!r}, "
                    f"court_identifier={court_identifier!r}"
                )

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
