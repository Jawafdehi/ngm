"""
Kanun Patrika (Nepal Law Journal) Spider

Scrapes PDF files of Nepal Law Journal from Supreme Court website.
"""

import os
import scrapy
from ngm.ngscrape.settings import FILES_STORE


class KanunPatrikaSpider(scrapy.Spider):
    """Spider for scraping Kanun Patrika (Nepal Law Journal) PDFs."""

    name = "kanun_patrika"
    allowed_domains = ["supremecourt.gov.np"]
    start_urls = ["https://supremecourt.gov.np/web/nkpold"]

    custom_settings = {
        "ITEM_PIPELINES": {
            "ngm.ngscrape.pipelines.KanunPatrikaPipeline": 1,
        },
        "FILES_STORE": os.path.join(FILES_STORE, "supreme-court/kanun-patrika/"),
    }

    def parse(self, response):
        """Parse the main page and extract PDF links with metadata."""
        rows = response.xpath(
            '//div[@class="content-wrap"]//table[@class="table-striped"]//tbody/tr'
        )
        self.logger.info(f"Found {len(rows)} rows")

        for row in rows:
            year = row.xpath(".//td[2]/text()").get("").strip()
            month = row.xpath(".//td[3]/text()").get("").strip()
            volume = row.xpath(".//td[4]/text()").get("").strip()
            issue = row.xpath(".//td[5]/text()").get("").strip()
            pdf_url = row.xpath('.//a[contains(@href, ".pdf")]/@href').get()

            if pdf_url:
                yield {
                    "file_urls": [response.urljoin(pdf_url)],
                    "metadata": {
                        "year": year,
                        "month": month,
                        "volume": volume,
                        "issue": issue,
                    },
                }
