"""
CIAA Press Releases Spider
Scrapes press releases from CIAA website by iterating through press release IDs.

Each press release has a unique ID from 1 to N. When N is sufficiently large and
there's no press release, CIAA redirects using HTTP 302 status code.

Features:
- Auto-discovery: Automatically finds all press releases
- Checkpointing: Resumes from last processed ID on subsequent runs
- Smart stopping: Stops after 10 consecutive missing press releases (404/302)
- Incremental updates: Run periodically to get new press releases

Usage:
    # Get all press releases (auto-stops on consecutive 302/404s)
    scrapy crawl ciaa_press_releases

    # Start from specific ID (useful for re-scraping or testing)
    scrapy crawl ciaa_press_releases -a start_id=3000

"""

import os
import re

import scrapy
from urllib.parse import urljoin

from ngm.ngscrape.settings import FILES_STORE
from ngm.utils.normalizer import nepali_to_roman_numerals, normalize_whitespace


# Base URL for CIAA press releases
BASE_URL = "https://ciaa.gov.np/pressrelease/"


class CiaaPressReleasesSpider(scrapy.Spider):
    name = "ciaa_press_releases"
    allowed_domains = ["ciaa.gov.np"]

    custom_settings = {
        "ITEM_PIPELINES": {
            "ngm.ngscrape.pipelines.CiaaPressReleasesPipeline": 1,
        },
        "FILES_STORE": os.path.join(FILES_STORE, "ciaa/press-releases/"),
        "MEDIA_ALLOW_REDIRECTS": False,  # Don't follow redirects for missing press releases
    }

    def __init__(self, start_id=None, *args, **kwargs):
        """
        Initialize spider with optional starting ID.

        Args:
            start_id: Starting press release ID (default: 1, or resume from checkpoint)

        Note: Spider automatically stops after 10 consecutive 302s/404s.
        """
        super().__init__(*args, **kwargs)

        self.checkpoint_file = os.path.join(
            FILES_STORE, "ciaa/press-releases/.checkpoint"
        )
        self.processed_ids = self._load_checkpoint()

        # Initialize current_id (source of truth for scraping position)
        if start_id is not None:
            self.current_id = int(start_id)
        elif self.processed_ids:
            self.current_id = max(self.processed_ids) + 1
            self.logger.info(f"Resuming from press release ID {self.current_id}")
        else:
            self.current_id = 1
            self.logger.info("Starting from press release ID 1")

        self.consecutive_missing = 0
        self.max_consecutive_missing = 10
        self._should_stop = False

    def _load_checkpoint(self):
        """Load processed press release IDs from checkpoint file.

        TODO: For GitHub Actions, implement S3-based checkpointing since local files don't persist
        """
        if os.path.exists(self.checkpoint_file):
            try:
                with open(self.checkpoint_file, "r") as f:
                    processed = {int(line.strip()) for line in f if line.strip()}
                self.logger.info(
                    f"Loaded checkpoint: {len(processed)} press releases already processed"
                )
                return processed
            except Exception as e:
                self.logger.warning(f"Failed to load checkpoint: {e}")
        return set()

    def _save_checkpoint(self, press_id):
        """Save processed press release ID to checkpoint file.

        TODO: For GitHub Actions, this local file approach doesn't work since files don't persist.
        """
        try:
            os.makedirs(os.path.dirname(self.checkpoint_file), exist_ok=True)
            with open(self.checkpoint_file, "a") as f:
                f.write(f"{press_id}\n")
        except Exception as e:
            self.logger.exception(f"Failed to save checkpoint for ID {press_id}")

    async def start(self):
        """
        Async start method for Scrapy 2.13+ compatibility.

        Sets language to Nepali first, then starts sequential scraping.
        """
        # Set language to Nepali for better title extraction
        yield scrapy.Request(
            url="https://ciaa.gov.np/changeLang/1",
            callback=self._start_scraping,
            dont_filter=True,
        )

    def _start_scraping(self, response):
        """
        Start scraping after setting language to Nepali.

        Simply logs and yields the first request via _next_request().
        """
        self.logger.info(f"Starting from press release ID {self.current_id}")
        yield from self._next_request()

    def _next_request(self):
        """Generate the next press release request."""
        if self._should_stop:
            return

        # Skip already processed IDs
        while self.current_id in self.processed_ids:
            self.logger.debug(
                f"Skipping press release {self.current_id} (already processed)"
            )
            self.current_id += 1

        yield scrapy.Request(
            url=f"{BASE_URL}{self.current_id}",
            callback=self.parse,
            meta={
                "press_id": self.current_id,
                "dont_redirect": True,
                "handle_httpstatus_all": True,  # Receive all status codes in parse()
            },
            errback=self.handle_error,
        )

    def handle_error(self, failure):
        """Handle request failures (network errors, timeouts, redirects, etc)."""
        press_id = failure.request.meta.get("press_id")

        # Try to get the response status if available
        if hasattr(failure.value, "response") and failure.value.response:
            status = failure.value.response.status
            self.logger.error(
                f"Request failed for press release {press_id}: "
                f"HTTP {status} ({failure.type.__name__})"
            )
            reason = f"HTTP {status}"
        else:
            self.logger.error(
                f"Request failed for press release {press_id}: "
                f"{failure.type.__name__} - {failure.value}"
            )
            reason = f"network error: {failure.type.__name__}"

        # Treat errors as missing press releases
        if press_id:
            self._handle_missing(press_id, reason)

        # Generate next request to continue scraping
        self.current_id += 1
        yield from self._next_request()

    def _handle_missing(self, press_id: int, reason: str):
        """
        Handle a missing press release (302 redirect or 4xx error).

        Increments the consecutive-missing counter, checkpoints the ID, and
        signals spider to stop once the threshold is reached.
        """
        self.consecutive_missing += 1
        self._save_checkpoint(press_id)
        self.logger.debug(
            f"Press release {press_id} not found ({reason}) — "
            f"consecutive missing: {self.consecutive_missing}/{self.max_consecutive_missing}"
        )

        if self.consecutive_missing >= self.max_consecutive_missing:
            last_valid = press_id - self.max_consecutive_missing
            self.logger.warning(
                f"Reached {self.max_consecutive_missing} consecutive missing press releases. "
                f"Last valid ID was likely around {last_valid}. Stopping spider."
            )
            self._should_stop = True
            self.crawler.engine.close_spider(
                self, f"Reached end of press releases at ID ~{last_valid}"
            )

    def parse(self, response):
        """Parse press release page and generate next request."""
        press_id = response.meta["press_id"]

        if response.status == 302:
            self._handle_missing(press_id, "302 redirect")
            self.current_id += 1
            yield from self._next_request()
            return

        if response.status >= 400:
            self._handle_missing(press_id, f"HTTP {response.status}")
            self.current_id += 1
            yield from self._next_request()
            return

        # Successful response — reset consecutive-missing counter
        self.consecutive_missing = 0

        # Title — two possible formats
        title = response.xpath('//div[@class="col-sm-8"]//h4//strong/text()').get()
        if not title:
            title = response.xpath('//div[@class="col-sm-8"]//h4/text()').get()
        title = normalize_whitespace(title or "")

        # Full text - get all text from divs and paragraphs, excluding badges (download links)
        # and social media elements
        content_parts = response.xpath(
            '//div[@class="col-sm-8"]//div[not(contains(@class, "fb-"))]//text() | '
            '//div[@class="col-sm-8"]//p//text()'
        ).getall()

        full_text = "\n".join(
            normalize_whitespace(part)
            for part in content_parts
            if part.strip() and part.strip() not in ("Download", "Tweet", "डाउनलोड")
        )

        # Download links
        download_links = response.xpath(
            '//div[@class="col-sm-8"]'
            '//a[contains(@class, "badge") and contains(@href, "/uploads/")]/@href'
        ).getall()
        file_urls = [urljoin(response.url, link) for link in download_links]

        publication_date = self.guess_publication_date(title + "\n" + full_text)

        self.logger.info(
            f"Press release {press_id}: {title[:50]}... "
            f"({len(file_urls)} files, date: {publication_date})"
        )

        self._save_checkpoint(press_id)

        yield {
            "file_urls": file_urls,
            "metadata": {
                "press_id": press_id,
                "title": title,
                "full_text": full_text,
                "publication_date": publication_date,
                "source_url": response.url,
            },
        }

        # Generate next request to continue the chain
        self.current_id += 1
        yield from self._next_request()

    def guess_publication_date(self, text: str) -> str:
        """
        Extract publication date from press release text.

        Handles patterns such as:
        - "Press Release- 2072-08-15"
        - "प्रेस विज्ञप्ति :- मिति २०७९।१२।१२ गते ।"
        - "प्रेस विज्ञप्ति २०७२/०८/१६"
        - "मिति २०८१/०९/२८"

        Returns:
            Date string in YYYY-MM-DD (BS) format, or empty string if not found.
        """
        if not text:
            return ""

        text_roman = nepali_to_roman_numerals(text)

        # "Press Release- 2072-08-15" / "Press Release 2072/08/15"
        match = re.search(
            r"Press\s+Release[-\s]*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})",
            text_roman,
            re.IGNORECASE,
        )
        if match:
            y, m, d = match.groups()
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"

        # "मिति २०७९।१२।१२ गते"
        match = re.search(
            r"मिति\s*(\d{4})[।./\-]\s*(\d{1,2})[।./\-]\s*(\d{1,2})", text_roman
        )
        if match:
            y, m, d = match.groups()
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"

        # "प्रेस विज्ञप्ति २०७२/०८/१६"
        match = re.search(
            r"प्रेस\s*विज्ञप्ति\s*(\d{4})[।./\-]\s*(\d{1,2})[।./\-]\s*(\d{1,2})",
            text_roman,
        )
        if match:
            y, m, d = match.groups()
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"

        # Bare date at the start of text "२०८१/०९/२८"
        match = re.search(
            r"^(\d{4})[।./\-]\s*(\d{1,2})[।./\-]\s*(\d{1,2})",
            text_roman.strip(),
        )
        if match:
            y, m, d = match.groups()
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"

        return ""
