import json
import os
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
        """Log download results."""
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

        # Get downloaded file path from results
        for ok, result in results:
            if ok:
                file_path = result["path"]

        # Save only essential metadata
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

            info.spider.logger.info(f"Saved  metadata: {metadata_path}")

        return item
