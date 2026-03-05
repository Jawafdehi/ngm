import json
import posixpath
from io import BytesIO
from urllib.parse import urlparse
from scrapy.pipelines.files import FilesPipeline


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
        """Generate custom file path based on metadata."""
        metadata = item.get("metadata", {})

        # Extract file_id from URL, handling query strings
        url_path = urlparse(request.url).path
        file_id = posixpath.splitext(posixpath.basename(url_path))[0]

        if metadata:
            serial_number = metadata.get("serial_number", "")
            title = metadata.get("title", "").replace("/", "-")
            # Clean title for filename
            safe_title = "".join(
                c for c in title if c.isalnum() or c in (" ", "-", "_")
            ).strip()
            if safe_title:
                return posixpath.join(
                    "pdf", f"{serial_number}. {safe_title} - {file_id}.pdf"
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

            json_file_path = file_path.replace("pdf/", "metadata/", 1).replace(
                ".pdf", ".json", 1
            )
            json_bytes = json.dumps(simple_meta, ensure_ascii=False, indent=2).encode(
                "utf-8"
            )

            try:
                self.store.persist_file(json_file_path, BytesIO(json_bytes), info)
                info.spider.logger.info(f"Saved metadata: {json_file_path}")
            except Exception as e:
                info.spider.logger.error(
                    f"Failed to save metadata {json_file_path}: {e}"
                )

        return item
