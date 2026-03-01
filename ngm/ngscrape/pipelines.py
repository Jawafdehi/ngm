import json
import os
from datetime import datetime
from urllib.parse import urlparse
from scrapy.pipelines.files import FilesPipeline
from sqlalchemy.orm.attributes import flag_modified
from ngm.database.models import CourtCase


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
            results: List of (success, result_dict) tuples from file downloads
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


class CiaaAnnualReportsPipeline(FilesPipeline):
    """Pipeline for downloading CIAA Annual Reports PDF files with metadata."""

    def file_path(self, request, response=None, info=None, *, item=None):
        """Generate custom file path based on metadata with PDFs in pdf/ subdirectory."""
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
                # Store PDFs in pdf/ subdirectory
                return f"pdf/{serial_number}. {safe_title} - {file_id}.pdf"

        # Store PDFs in pdf/ subdirectory
        return f"pdf/{file_id}.pdf"

    def item_completed(self, results, item, info):
        """
        Save JSON metadata and log upload results for CIAA annual reports.

        Creates JSON metadata files alongside PDFs, storing them in the metadata/
        subdirectory. Supports both local filesystem and S3/R2 remote storage.

        Args:
            results: List of (success, result_dict) tuples from file downloads
            item: The scraped item containing metadata and file URLs
            info: Spider information object with settings and logger

        Returns:
            The processed item with upload status logged
        """
        metadata = item.get("metadata", {})
        files_store = info.spider.settings.get("FILES_STORE")

        for ok, result in results:
            if ok:
                file_path = result["path"]
                info.spider.logger.info(f"Uploaded: {file_path}")

                # Save JSON metadata in metadata/ subdirectory
                if metadata and file_path.endswith(".pdf"):
                    simple_meta = {
                        "serial_number": metadata.get("serial_number", ""),
                        "date": metadata.get("date", ""),
                        "title": metadata.get("title", ""),
                        "file_name": os.path.basename(file_path),
                    }

                    # Replace pdf/ with metadata/ and .pdf with .json
                    json_file_path = file_path.replace("pdf/", "metadata/").replace(
                        ".pdf", ".json"
                    )

                    # Check if FILES_STORE is remote (S3/R2)
                    if files_store and files_store.startswith("s3://"):
                        # For S3/R2, upload JSON using boto3 directly
                        try:
                            import boto3

                            # Parse S3 URL
                            parsed_store = urlparse(files_store)
                            bucket_name = parsed_store.netloc
                            prefix = parsed_store.path.lstrip("/")

                            # Construct full S3 key (use forward slashes for S3, not os.path.join)
                            s3_key = (
                                f"{prefix}/{json_file_path}"
                                if prefix
                                else json_file_path
                            )

                            # Get S3 client with custom endpoint if configured
                            s3_config = {
                                "aws_access_key_id": os.getenv("AWS_ACCESS_KEY_ID"),
                                "aws_secret_access_key": os.getenv(
                                    "AWS_SECRET_ACCESS_KEY"
                                ),
                                "region_name": os.getenv("AWS_REGION", "auto"),
                            }

                            endpoint_url = os.getenv("AWS_ENDPOINT_URL")
                            if endpoint_url:
                                s3_config["endpoint_url"] = endpoint_url

                            s3_client = boto3.client("s3", **s3_config)

                            # Upload JSON to S3
                            json_content = json.dumps(
                                simple_meta, ensure_ascii=False, indent=2
                            )
                            s3_client.put_object(
                                Bucket=bucket_name,
                                Key=s3_key,
                                Body=json_content.encode("utf-8"),
                                ContentType="application/json",
                            )

                            info.spider.logger.info(f"Uploaded: {json_file_path}")
                        except Exception as e:
                            info.spider.logger.exception(
                                f"Failed to upload JSON metadata to S3: {e}"
                            )
                    elif files_store and files_store.startswith(("gs://", "ftp://")):
                        info.spider.logger.warning(
                            f"JSON metadata upload is not implemented for store: {files_store}"
                        )
                    else:
                        # For local filesystem
                        try:
                            metadata_path = os.path.join(files_store, json_file_path)
                            os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
                            with open(metadata_path, "w", encoding="utf-8") as f:
                                json.dump(simple_meta, f, ensure_ascii=False, indent=2)
                            info.spider.logger.info(f"Uploaded: {json_file_path}")
                        except Exception as e:
                            info.spider.logger.warning(
                                f"Failed to save JSON metadata locally: {e}"
                            )
            else:
                info.spider.logger.error(f"Failed to upload: {result}")

        return item


class SupremeCourtOrdersPipeline(FilesPipeline):
    """
    Pipeline for downloading Supreme Court order documents and saving metadata.

    NOTE: The enrichment spider (supreme_orders_enrichment.py) does NOT use this
    pipeline - it handles file writing directly. This pipeline exists for any
    future spider that yields items with file_urls for court order documents.
    """

    def open_spider(self, spider):
        """
        Initialize pipeline when spider opens.

        Obtains database session from spider if available for tracking
        download status in the database.

        Args:
            spider: The spider instance being opened
        """
        super().open_spider(spider)

        if hasattr(spider, "session"):
            self.session = spider.session
            spider.logger.info("Pipeline initialized with database session")
        else:
            self.session = None
            spider.logger.warning("Spider has no database session")

    def close_spider(self, spider):
        """
        Clean up pipeline when spider closes.

        Note: Database session is managed by the spider, not the pipeline.

        Args:
            spider: The spider instance being closed
        """
        if self.session:
            spider.logger.info("Pipeline database session will be closed by spider")
        super().close_spider(spider)

    def file_path(self, request, response=None, info=None, *, item=None):
        """
        Generate file path relative to FILES_STORE.
        Format: court/orders/<court_identifier>/<case_number>/file_1.{extension}
        """
        court_identifier = item.get("court_identifier", "unknown")
        case_number = item.get("case_number", "unknown").replace("/", "-")

        file_ext = item.get("file_extension")
        if file_ext and not file_ext.startswith("."):
            file_ext = f".{file_ext}"
        if not file_ext:
            # Parse URL path to avoid including query strings in extension
            url_path = urlparse(request.url).path
            file_ext = os.path.splitext(url_path)[1]
        if not file_ext:
            file_ext = ".doc"

        # TODO(GitHub #XXX): Support multiple files per case (file_2, file_3, etc.)
        filename = f"file_1{file_ext}"

        return os.path.join("court", "orders", court_identifier, case_number, filename)

    def item_completed(self, results, item, info):
        """
        Update database status and save metadata JSON after download.

        Processes download results, updates the CourtCase database record with
        success/failure status, and saves metadata JSON file for local storage.

        Args:
            results: List of (success, result_dict) tuples from file downloads
            item: The scraped item containing case information and document URL
            info: Spider information object with settings and logger

        Returns:
            The processed item with database and metadata updates completed
        """
        file_path = None
        download_success = False
        error_message = None

        for ok, result in results:
            if ok:
                file_path = result["path"]
                download_success = True
                info.spider.logger.info(f"Downloaded: {file_path}")
            else:
                error_message = str(result)
                info.spider.logger.error(
                    f"Failed to download: {item.get('document_url')} - {error_message}"
                )

        if self.session:
            try:
                case_number = item.get("case_number")
                court_identifier = item.get("court_identifier")

                if case_number and court_identifier:
                    with self.session.begin_nested():
                        case = (
                            self.session.query(CourtCase)
                            .filter_by(
                                case_number=case_number,
                                court_identifier=court_identifier,
                            )
                            .first()
                        )

                        if case:
                            if case.extra_data is None:
                                case.extra_data = {}

                            if download_success:
                                case.extra_data["orders_scraped"] = True
                                case.extra_data["orders_scraped_at"] = (
                                    datetime.now().isoformat()
                                )
                                case.extra_data["orders_file_path"] = file_path
                                case.status = "enriched"
                                flag_modified(case, "extra_data")
                                info.spider.logger.info(
                                    f"Updated database: {case_number} - orders_scraped=true"
                                )
                            else:
                                case.extra_data["orders_failed"] = True
                                case.extra_data["orders_error"] = (
                                    error_message or "Download failed"
                                )
                                case.extra_data["orders_failed_at"] = (
                                    datetime.now().isoformat()
                                )
                                flag_modified(case, "extra_data")
                                info.spider.logger.info(
                                    f"Updated database: {case_number} - orders_failed=true"
                                )
                        else:
                            info.spider.logger.warning(
                                f"Case not found in database: {case_number}"
                            )

            except Exception as e:
                info.spider.logger.exception(f"Error updating database: {e}")

        if file_path:
            metadata = {
                "case_number": item.get("case_number"),
                "court_identifier": item.get("court_identifier"),
                "document_url": item.get("document_url"),
                "scraped_at": item.get("scraped_at"),
            }

            files_store = info.spider.settings.get("FILES_STORE")

            # Guard: don't attempt os.makedirs on S3/GS paths
            if files_store and not files_store.startswith(("s3://", "gs://", "ftp://")):
                file_dir = os.path.dirname(file_path)
                metadata_path = os.path.join(files_store, file_dir, "metadata.json")

                try:
                    os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
                    with open(metadata_path, "w", encoding="utf-8") as f:
                        json.dump(metadata, f, ensure_ascii=False, indent=2)
                    info.spider.logger.info(f"Saved metadata: {metadata_path}")
                except Exception as e:
                    info.spider.logger.warning(f"Failed to save metadata: {e}")
            else:
                # TODO(GitHub #XXX): Implement S3 metadata storage
                info.spider.logger.debug(
                    f"Skipping metadata save for remote storage: {files_store}"
                )

        return item
