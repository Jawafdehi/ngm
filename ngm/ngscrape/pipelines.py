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


class SupremeCourtOrdersPipeline(FilesPipeline):
    """Pipeline for downloading Supreme Court order documents and saving metadata."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.s3_client = None
        self.is_s3_store = False
        self.s3_bucket = None
        self.s3_prefix = None
    
    def open_spider(self, spider):
        """Initialize S3 client if using S3 storage."""
        super().open_spider(spider)
        files_store = spider.settings.get('FILES_STORE')
        
        if files_store and files_store.startswith('s3://'):
            self.is_s3_store = True
            # Parse bucket and prefix from s3://bucket/prefix
            parts = files_store.replace('s3://', '').split('/', 1)
            self.s3_bucket = parts[0]
            self.s3_prefix = parts[1] if len(parts) > 1 else ''
            
            # Initialize S3 client
            aws_access_key = spider.settings.get('AWS_ACCESS_KEY_ID')
            aws_secret_key = spider.settings.get('AWS_SECRET_ACCESS_KEY')
            aws_endpoint = spider.settings.get('AWS_ENDPOINT_URL')
            aws_region = spider.settings.get('AWS_REGION', 'auto')
            
            self.s3_client = boto3.client(
                's3',
                aws_access_key_id=aws_access_key,
                aws_secret_access_key=aws_secret_key,
                endpoint_url=aws_endpoint,
                region_name=aws_region
            )
            spider.logger.info(f"S3 storage initialized: {self.s3_bucket}/{self.s3_prefix}")
    
    def file_path(self, request, response=None, info=None, *, item=None):
        """Generate custom file path based on case metadata."""
        registration_no = item.get('registration_no', '').replace('/', '-')
        case_no = item.get('case_no', '')
        file_ext = os.path.splitext(request.url)[1] or '.doc'
        
        # Create filename: registration_no - case_no.ext
        if registration_no:
            filename = f"{registration_no} - {case_no}{file_ext}"
        else:
            filename = f"{case_no}{file_ext}"
        
        return filename
    
    def item_completed(self, results, item, info):
        """Save metadata JSON alongside the downloaded file."""
        # Get downloaded file path from results
        file_path = None
        for ok, result in results:
            if ok:
                file_path = result['path']
                info.spider.logger.info(f"Downloaded: {file_path}")
            else:
                info.spider.logger.error(f"Failed to download: {item.get('document_url')}")
        
        # Save metadata JSON
        if file_path:
            metadata = {
                'serial_no': item.get('serial_no'),
                'registration_no': item.get('registration_no'),
                'case_no': item.get('case_no'),
                'registration_date': item.get('registration_date'),
                'case_type': item.get('case_type'),
                'case_name': item.get('case_name'),
                'plaintiff': item.get('plaintiff'),
                'defendant': item.get('defendant'),
                'decision_date': item.get('decision_date'),
                'document_url': item.get('document_url'),
                'court_type': item.get('court_type'),
                'source_url': item.get('source_url'),
                'scraped_at': item.get('scraped_at'),
            }
            
            metadata_filename = file_path.replace(os.path.splitext(file_path)[1], '.json')
            metadata_json = json.dumps(metadata, ensure_ascii=False, indent=2)
            
            if self.is_s3_store:
                # Upload metadata to S3
                s3_key = os.path.join(self.s3_prefix, metadata_filename).replace('\\', '/')
                try:
                    self.s3_client.put_object(
                        Bucket=self.s3_bucket,
                        Key=s3_key,
                        Body=metadata_json.encode('utf-8'),
                        ContentType='application/json'
                    )
                    info.spider.logger.info(f"Saved metadata to S3: s3://{self.s3_bucket}/{s3_key}")
                except Exception as e:
                    info.spider.logger.error(f"Failed to upload metadata to S3: {e}")
            else:
                # Save metadata locally
                files_store = info.spider.settings.get('FILES_STORE')
                metadata_path = os.path.join(files_store, metadata_filename)
                os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
                
                with open(metadata_path, 'w', encoding='utf-8') as f:
                    f.write(metadata_json)
                
                info.spider.logger.info(f"Saved metadata: {metadata_path}")
        
        return item
