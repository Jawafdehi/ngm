import json
import os
import re
import posixpath
import uuid
from datetime import datetime
from io import BytesIO
from urllib.parse import urlparse
import pytz
from scrapy.pipelines.files import FilesPipeline
from sqlalchemy.orm.attributes import flag_modified
from ngm.database.models import CourtCase

KATHMANDU_TZ = pytz.timezone("Asia/Kathmandu")

# Days since last hearing before documents are expected on the site
MIN_DAYS_FOR_DOCUMENT_AVAILABILITY = 365

# How often to re-check soft-skipped cases
TOO_RECENT_RECHECK_DAYS = 30


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
        """
        Process completed file downloads and log results.

        Args:
            results: List where each entry is either (True, result_dict) for
                successful downloads or (False, Failure) for failed downloads.
                Failure objects have getErrorMessage() method for error details.
            item: The scraped item containing metadata
            info: Spider information object

        Returns:
            The processed item
        """
        for ok, result in results:
            if ok:
                file_path = result["path"]
                info.spider.logger.info(f"Downloaded: {file_path}")
            else:
                info.spider.logger.error(f"Failed: {item['file_urls'][0]}")
        return item


class CiaaPressReleasesPipeline(FilesPipeline):
    """Pipeline for downloading CIAA Press Releases files with metadata."""

    # Filesystem-unsafe chars. Denylist preserves Devanagari combining marks.
    _UNSAFE_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|\x00]')

    def _safe_filename(self, text: str, max_bytes: int = 230) -> str:
        """Strip unsafe chars and truncate to max_bytes of UTF-8 (Devanagari = 3 bytes/char)."""
        cleaned = self._UNSAFE_FILENAME_CHARS.sub("", text).strip()
        encoded = cleaned.encode("utf-8")
        if len(encoded) > max_bytes:
            cleaned = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return cleaned.rstrip(
            " ._-"
        )  # remove trailing punctuation left by byte truncation

    def file_path(self, request, response=None, info=None, *, item=None):
        """Generate custom file path based on metadata."""
        metadata = item.get("metadata", {}) if item else {}
        url_path = urlparse(request.url).path
        file_name = posixpath.basename(url_path)
        _file_id, ext = posixpath.splitext(file_name)

        press_id = metadata.get("press_id", "")
        title = metadata.get("title", "").replace("/", "-")
        safe_title = self._safe_filename(title)

        # Determine 1-based file number by position in file_urls.
        # Avoid list.index() — raises ValueError on duplicates and is O(n).
        all_urls = item.get("file_urls", []) if item else []
        file_num = next(
            (i + 1 for i, u in enumerate(all_urls) if u == request.url),
            1,
        )

        if safe_title:
            filename = f"{press_id}. {safe_title} - {file_num}{ext}"
        else:
            filename = f"{press_id} - {file_num}{ext}"

        return posixpath.join("ciaa", "press-releases", "files", filename)

    def item_completed(self, results, item, info):
        """Save metadata and log download results."""
        metadata = item.get("metadata", {})
        press_id = metadata.get("press_id", "")
        all_urls = item.get("file_urls", [])

        file_names = []
        for i, (ok, result) in enumerate(results):
            if ok:
                fp = result["path"]
                file_names.append(posixpath.basename(fp))
                info.spider.logger.info(f"[{press_id}] Downloaded: {fp}")
            else:
                url = all_urls[i] if i < len(all_urls) else "<unknown>"
                error_msg = (
                    result.getErrorMessage()
                    if hasattr(result, "getErrorMessage")
                    else str(result)
                )
                info.spider.logger.error(
                    f"[{press_id}] Failed to download {url}: {error_msg}"
                )

        if metadata:
            simple_meta = {
                "press_id": press_id,
                "title": metadata.get("title", ""),
                "publication_date": metadata.get("publication_date", ""),
                "full_text": metadata.get("full_text", ""),
                "source_url": metadata.get("source_url", ""),
                "file_names": file_names,
            }

            # Use same naming convention as files for consistency
            safe_title = self._safe_filename(
                metadata.get("title", "").replace("/", "-")
            )
            if safe_title:
                json_filename = f"{press_id}. {safe_title}.json"
            else:
                json_filename = f"{press_id}.json"

            json_file_path = posixpath.join(
                "ciaa", "press-releases", "metadata", json_filename
            )
            json_bytes = json.dumps(simple_meta, ensure_ascii=False, indent=2).encode(
                "utf-8"
            )

            try:
                self.store.persist_file(json_file_path, BytesIO(json_bytes), info)
                info.spider.logger.info(
                    f"[{press_id}] Saved metadata: {json_file_path}"
                )

                # Save checkpoint after successful metadata save (durable output watermark)
                # Don't write checkpoints during backfill runs to prevent regressing the watermark
                if hasattr(info.spider, "_save_checkpoint") and not getattr(
                    info.spider, "_is_backfill", False
                ):
                    info.spider._save_checkpoint(press_id)

            except Exception:
                info.spider.logger.exception(
                    f"[{press_id}] Failed to save metadata {json_file_path}"
                )

        return item


class CiaaAnnualReportsPipeline(FilesPipeline):
    """Pipeline for downloading CIAA Annual Reports PDF files with metadata."""

    def file_path(self, request, response=None, info=None, *, item=None):
        """Generate custom file path based on metadata."""
        metadata = item.get("metadata", {})

        # Extract file_id from URL, handling query strings
        url_path = urlparse(request.url).path
        file_id = posixpath.splitext(posixpath.basename(url_path))[0]

        if metadata:
            serial_number = metadata.get("serial_number", "")
            title = metadata.get("title", "").replace("/", "-")

            # Sanitize serial_number to prevent path traversal
            safe_serial = "".join(
                c for c in str(serial_number) if c.isalnum() or c in (" ", "-", "_")
            ).strip()

            # Clean title for filename
            safe_title = "".join(
                c for c in title if c.isalnum() or c in (" ", "-", "_")
            ).strip()

            if safe_title:
                serial_prefix = f"{safe_serial}. " if safe_serial else ""
                return posixpath.join(
                    "pdf", f"{serial_prefix}{safe_title} - {file_id}.pdf"
                )

        return posixpath.join("pdf", f"{file_id}.pdf")

    def item_completed(self, results, item, info):
        """Save simplified metadata and log download results."""
        metadata = item.get("metadata", {})

        file_path = None

        for ok, result in results:
            if ok:
                file_path = result["path"]
                info.spider.logger.info(f"Downloaded: {file_path}")
            else:
                error_msg = (
                    result.getErrorMessage()
                    if hasattr(result, "getErrorMessage")
                    else str(result)
                )
                info.spider.logger.error(
                    f"Failed to download {item['file_urls'][0]}: {error_msg}"
                )

        if metadata and file_path:
            simple_meta = {
                "serial_number": metadata.get("serial_number", ""),
                "date": metadata.get("date", ""),
                "title": metadata.get("title", ""),
                "file_name": posixpath.basename(file_path),
            }

            # Safely construct JSON path by replacing directory and extension
            metadata_rel_path = (
                f"metadata/{file_path[len('pdf/'):]}"
                if file_path.startswith("pdf/")
                else file_path
            )
            json_file_path = posixpath.splitext(metadata_rel_path)[0] + ".json"

            json_bytes = json.dumps(simple_meta, ensure_ascii=False, indent=2).encode(
                "utf-8"
            )

            try:
                self.store.persist_file(json_file_path, BytesIO(json_bytes), info)
                info.spider.logger.info(f"Saved metadata: {json_file_path}")
            except Exception:
                info.spider.logger.exception(
                    f"Failed to save metadata {json_file_path}"
                )

        return item


class SupremeCourtOrdersPipeline(FilesPipeline):
    """
    Pipeline for Supreme Court order documents.

    Item error flags from spider:
    "too_recent"   → soft skip, re-check in TOO_RECENT_RECHECK_DAYS days
    anything else  → permanent failure
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

    def _now_iso(self):
        """Return current timestamp in Kathmandu timezone as ISO string."""
        return datetime.now(KATHMANDU_TZ).replace(tzinfo=None).isoformat()

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
            if failed_results:
                info.spider.logger.warning(
                    f"[{case_number}] Partial download: {len(successful_paths)} succeeded, "
                    f"{len(failed_results)} failed and will not be retried. "
                    f"Failures: {'; '.join(failed_results)}"
                )
            if self._mark_success(
                info.spider, case_number, court_identifier, successful_paths
            ):
                info.spider.successful_cases += 1
            else:
                # Case not found in DB - files saved to S3 but DB not updated
                info.spider.logger.error(
                    f"[{case_number}] DB persistence failed (case not found). "
                    f"Files saved to S3 but not tracked in database."
                )
                info.spider.failed_cases += 1
        elif spider_error == "too_recent":
            # Recent case — soft skip, pipeline will re-check in TOO_RECENT_RECHECK_DAYS days
            self._mark_too_recent(info.spider, case_number, court_identifier)
            # Not a failure — do not increment failed_cases
        elif spider_error in ("no_docs_old_case", "no_records_old_case"):
            # Spider-emitted permanent reasons: the site genuinely has no document
            # for an old, decided case. Exclude from future runs.
            self._mark_failed(info.spider, case_number, court_identifier, spider_error)
            info.spider.failed_cases += 1
        else:
            # Transient: a FilesPipeline download/upload failure (FileException /
            # timeout / S3 error) or any other unrecognised condition. Do NOT mark
            # orders_failed — leave the case re-crawlable so the next run retries it.
            error = spider_error or "; ".join(failed_results) or "Unknown failure"
            self._mark_transient(info.spider, case_number, court_identifier, error)
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

        Returns:
            bool: True if database update succeeded, False if case not found
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
                    return False

                # Initialize extra_data if needed
                if case.extra_data is None:
                    case.extra_data = {}

                # Store file paths and timestamp
                case.extra_data["court_orders"] = file_paths
                case.extra_data["court_orders_scraped_at"] = self._now_iso()

                # Clear all previous state
                case.extra_data.pop("orders_failed", None)
                case.extra_data.pop("orders_error", None)
                case.extra_data.pop("orders_failed_at", None)
                case.extra_data.pop("orders_too_recent", None)
                case.extra_data.pop("orders_too_recent_checked_at", None)
                case.extra_data.pop("orders_transient_error", None)
                case.extra_data.pop("orders_transient_at", None)
                case.extra_data.pop("orders_transient_retries", None)

                # Mark field as modified for SQLAlchemy JSONB tracking
                flag_modified(case, "extra_data")
                return True
        except Exception:
            spider.logger.exception(
                f"[{case_number}] Unexpected error marking case as successful"
            )
            raise  # Re-raise to fail fast on DB errors

    def _mark_too_recent(self, spider, case_number, court_identifier):
        """
        Soft-skip: case is too recent for documents to be available yet.

        Writes orders_too_recent=true and orders_too_recent_checked_at=now.
        Selection query will re-pick this case after TOO_RECENT_RECHECK_DAYS days.
        Once a document appears, _mark_success clears these fields.
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
                        f"[{case_number}] Not in DB. Cannot mark as too_recent."
                    )
                    return

                if case.extra_data is None:
                    case.extra_data = {}

                case.extra_data["orders_too_recent"] = True
                case.extra_data["orders_too_recent_checked_at"] = self._now_iso()

                flag_modified(case, "extra_data")
                spider.logger.info(
                    f"[{case_number}] Marked too_recent. "
                    f"Will re-check in {TOO_RECENT_RECHECK_DAYS} days."
                )
        except Exception:
            spider.logger.exception(f"[{case_number}] Error marking too_recent")
            raise

    MAX_TRANSIENT_RETRIES = 5

    def _mark_transient(self, spider, case_number, court_identifier, error):
        """Transient failure (download/S3/timeout) — NON-terminal.

        Does not set orders_failed, so the selection query still re-picks the
        case next run. Tracks a retry counter and only escalates to a permanent
        failure after MAX_TRANSIENT_RETRIES so a genuinely-dead URL eventually
        stops being re-queued.
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
                        f"[{case_number}] Not in DB. Cannot mark transient."
                    )
                    return
                if case.extra_data is None:
                    case.extra_data = {}

                retries = int(case.extra_data.get("orders_transient_retries", 0)) + 1
                case.extra_data["orders_transient_retries"] = retries
                case.extra_data["orders_transient_error"] = error
                case.extra_data["orders_transient_at"] = self._now_iso()
                # A transient path must never leave a stale permanent flag.
                case.extra_data.pop("orders_failed", None)
                case.extra_data.pop("orders_error", None)
                case.extra_data.pop("orders_failed_at", None)

                if retries >= self.MAX_TRANSIENT_RETRIES:
                    case.extra_data["orders_failed"] = True
                    case.extra_data["orders_error"] = (
                        f"transient_exhausted after {retries} retries: {error}"
                    )
                    case.extra_data["orders_failed_at"] = self._now_iso()
                    spider.logger.error(
                        f"[{case_number}] Transient retries exhausted ({retries}) "
                        "— marking permanent."
                    )
                else:
                    spider.logger.warning(
                        f"[{case_number}] Transient download failure "
                        f"(retry {retries}/{self.MAX_TRANSIENT_RETRIES}) — "
                        "will retry next run."
                    )
                flag_modified(case, "extra_data")
        except Exception:
            spider.logger.exception(f"[{case_number}] Error marking transient")
            raise

    def _mark_failed(self, spider, case_number, court_identifier, error):
        """
        Permanent failure — excluded from all future runs unless manually cleared.
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
                        f"[{case_number}] Not in DB. Cannot mark as failed."
                    )
                    return

                if case.extra_data is None:
                    case.extra_data = {}

                case.extra_data["orders_failed"] = True
                case.extra_data["orders_error"] = error
                case.extra_data["orders_failed_at"] = self._now_iso()

                flag_modified(case, "extra_data")
        except Exception:
            spider.logger.exception(f"[{case_number}] Error marking failed")
            raise


class PPMOBlacklistPipeline(FilesPipeline):
    """Pipeline for handling PPMO Blacklisted Firms and writing metadata for index."""

    def _safe_filename(self, text: str, max_bytes: int = 230) -> str:
        """Sanitize filename."""
        cleaned = re.sub(r'[/\\:*?"<>|\x00]', "", text).strip()
        encoded = cleaned.encode("utf-8")
        if len(encoded) > max_bytes:
            cleaned = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return cleaned.rstrip(" ._-")

    def file_path(self, request, response=None, info=None, *, item=None):
        """Not downloading files here, but FilesPipeline requires this."""
        return super().file_path(request, response, info, item=item)

    def item_completed(self, results, item, info):
        """Save metadata and log results."""
        # PPMO doesn't have PDFs to download in the current implementation,
        # but we use FilesPipeline to get access to self.store for S3/R2.

        firm_name = item.get("firm_name", "")
        blacklist_date_bs = item.get("blacklist_date_bs", "")

        # Create a unique-ish filename
        safe_name = self._safe_filename(firm_name)
        json_filename = f"{blacklist_date_bs}_{safe_name}.json"

        json_file_path = posixpath.join("ppmo", "blacklist", "metadata", json_filename)

        metadata = {
            "firm_name": firm_name,
            "proprietor_name": item.get("proprietor_name"),
            "address": item.get("address"),
            "blacklist_date_bs": blacklist_date_bs,
            "blacklist_date_ad": (
                item.get("blacklist_date_ad").isoformat()
                if item.get("blacklist_date_ad")
                else None
            ),
            "effective_until_bs": item.get("effective_until_bs"),
            "effective_until_ad": (
                item.get("effective_until_ad").isoformat()
                if item.get("effective_until_ad")
                else None
            ),
            "duration": item.get("duration"),
            "reason": item.get("reason"),
            "recommending_office": item.get("recommending_office"),
            "source_url": item.get("source_url"),
        }

        json_bytes = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")

        try:
            self.store.persist_file(json_file_path, BytesIO(json_bytes), info)
            info.spider.logger.info(f"Saved PPMO metadata: {json_file_path}")
        except Exception:
            info.spider.logger.exception(
                f"Failed to save PPMO metadata {json_file_path}"
            )

        return item
