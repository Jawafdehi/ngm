"""
CIAA Annual Reports Spider
Scrapes PDF files of CIAA Annual Reports from CIAA website.

This spider relies on Scrapy's FilesPipeline for duplicate detection.
The pipeline automatically checks if files exist in storage (local or S3/R2) and skips
re-downloading files that were downloaded recently (within FILES_EXPIRES days, default 90).
"""

import os
import scrapy
from urllib.parse import urlparse

from ngm.ngscrape.settings import FILES_STORE


class CiaaAnnualReportsSpider(scrapy.Spider):
    name = "ciaa_annual_reports"
    allowed_domains = ["ciaa.gov.np"]
    start_urls = ["https://ciaa.gov.np/index.php/publications/7"]

    custom_settings = {
        "ITEM_PIPELINES": {
            "ngm.ngscrape.pipelines.CiaaAnnualReportsPipeline": 1,
        },
        "FILES_STORE": os.path.join(FILES_STORE, "ciaa/annual-reports/"),
        "MEDIA_ALLOW_REDIRECTS": True,
    }

    def get_site_root(self, response):
        parsed = urlparse(response.url)
        return f"{parsed.scheme}://{parsed.netloc}/"

    def parse(self, response):
        site_root = self.get_site_root(response)

        rows = response.xpath(
            '//table[@class="table table-hover table-bordered table-responsive"]//tbody/tr'
        )

        self.logger.info(f"Found {len(rows)} rows on page {response.url}")

        for row in rows:
            serial_number = row.xpath('.//th[@scope="row"]/text()').get("").strip()
            date = row.xpath(".//td[1]/p/text()").get("").strip()

            title_link = row.xpath('.//td[2]//div[@class="row"]/div[@class="col"]/a')
            title = title_link.xpath("./text()").get("").strip()
            detail_url = title_link.xpath("./@href").get()

            pdf_url = row.xpath(
                './/td[3]//a[contains(@class,"badge-danger")]/@href'
            ).get()

            if not pdf_url:
                continue

            pdf_url = pdf_url.strip()

            # Absolute URL
            if not pdf_url.startswith("http"):
                pdf_url = site_root + pdf_url.lstrip("/")

            # HARD STRIP index.php (CIAA CMS bug)
            pdf_url = pdf_url.replace("/index.php/", "/")

            yield {
                "file_urls": [pdf_url],
                "metadata": {
                    "serial_number": serial_number,
                    "date": date,
                    "title": title,
                    "detail_url": response.urljoin(detail_url) if detail_url else "",
                    "source_page": response.url,
                },
            }

        # Pagination
        next_page = response.xpath(
            '//ul[@class="pagination"]//li[@class="page-item"]/a[@rel="next"]/@href'
        ).get()

        if next_page:
            yield response.follow(next_page, callback=self.parse)
