import logging
from scrapy import signals


class SchemaChangeDetector:
    """
    Scrapy extension to detect potential schema changes on target websites.

    Alerts if a spider receives responses but fails to scrape any items,
    or if the yield rate is unusually low.
    """

    def __init__(self, stats):
        self.stats = stats
        self.logger = logging.getLogger(__name__)

    @classmethod
    def from_crawler(cls, crawler):
        ext = cls(crawler.stats)
        crawler.signals.connect(ext.spider_closed, signal=signals.spider_closed)
        return ext

    def spider_closed(self, spider):
        item_count = self.stats.get_value("item_scraped_count", 0)
        response_count = self.stats.get_value("response_received_count", 0)

        # If we got responses but no items, something is likely wrong with selectors
        if response_count > 0 and item_count == 0:
            self.logger.error(
                f"CRITICAL: POTENTIAL SCHEMA CHANGE DETECTED for {spider.name}. "
                f"Received {response_count} responses but scraped 0 items. "
                "Check website structure and spider selectors."
            )
        # If yield is extremely low (less than 5%), it might also be a partial change or error
        elif response_count > 10 and item_count < (response_count * 0.05):
            self.logger.warning(
                f"WARNING: LOW YIELD for {spider.name}. "
                f"Received {response_count} responses but scraped only {item_count} items. "
                "This may indicate a partial schema change or a high error rate."
            )
