"""
Supreme Court Orders Spider

- Find document URL via CAPTCHA-protected search form
- Yield {"file_urls": [...], "case_number": ..., "court_identifier": ...}
- FilesPipeline handles download + S3 upload
- Pipeline updates DB: extra_data["court_orders"] = [url1, url2, ...]

Courts: Special (priority 1) -> Supreme (priority 2) only
"""

import re
import os
import pytz
from collections import deque
from urllib.parse import urljoin, urlparse, unquote
from scrapy.http import FormRequest
from bs4 import BeautifulSoup
from sqlalchemy import or_

from ngm.database.models import get_engine, get_session, CourtCase
from ngm.utils.court_mapping import get_court_params
from ngm.ngscrape.settings import FILES_STORE

import scrapy

KATHMANDU_TZ = pytz.timezone("Asia/Kathmandu")


class SupremeCourtOrdersSpider(scrapy.Spider):
    name = "supreme_court_orders"
    allowed_domains = ("supremecourt.gov.np",)

    custom_settings = {  # noqa: RUF012
        "ITEM_PIPELINES": {
            "ngm.ngscrape.pipelines.SupremeCourtOrdersPipeline": 1,
        },
        "FILES_STORE": FILES_STORE,
        "CONCURRENT_REQUESTS": 1,
        "DOWNLOAD_DELAY": 3,
        "DOWNLOAD_TIMEOUT": 60,
        "COOKIES_ENABLED": True,
        "REDIRECT_ENABLED": False,
        "USER_AGENT": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "DEFAULT_REQUEST_HEADERS": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        },
    }

    def __init__(self, limit=500, *args, **kwargs):
        super().__init__(*args, **kwargs)

        try:
            self.limit = int(limit)
        except (ValueError, TypeError) as e:
            raise ValueError(f"limit must be a valid integer, got: {limit!r}") from e

        if self.limit <= 0:
            raise ValueError(f"limit must be positive, got: {self.limit}")

        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise ValueError("DATABASE_URL environment variable is not set")

        parsed = urlparse(db_url)
        username = parsed.username[:2] + "**" if parsed.username else "**"
        self.logger.info(f"DATABASE_URL set (user={username})")

        self.engine = get_engine(db_url)
        self.session = get_session(self.engine)

        self.total_cases = 0
        self.successful_cases = 0
        self.failed_cases = 0

    def _get_cases_to_scrape(self):
        """
        Query all decided cases that haven't been scraped yet.
        Only processes Special Court and Supreme Court cases.

        Priority order: Special (1) first, then Supreme (2).
        Within each priority group: most recent registration date first.
        """
        from sqlalchemy import case as sql_case

        query = self.session.query(CourtCase)

        # Only special and supreme courts
        query = query.filter(
            or_(
                CourtCase.court_identifier == "special",
                CourtCase.court_identifier == "supreme",
            )
        )

        # Has a final decision
        query = query.filter(CourtCase.case_status.like("%फैसला%"))

        # Not ongoing
        query = query.filter(
            ~CourtCase.case_status.like("%चालु%"),
            ~CourtCase.case_status.like("%चलिरहेको%"),
        )

        # Not already scraped
        query = query.filter(
            or_(
                CourtCase.extra_data.is_(None),
                CourtCase.extra_data["court_orders"].astext.is_(None),
            )
        )

        # Skip known failures
        query = query.filter(
            or_(
                CourtCase.extra_data.is_(None),
                CourtCase.extra_data["orders_failed"].astext.is_(None),
                CourtCase.extra_data["orders_failed"].astext != "true",
            )
        )

        # Priority: Special(1) → Supreme(2)
        court_priority = sql_case(
            (CourtCase.court_identifier == "special", 1),
            (CourtCase.court_identifier == "supreme", 2),
            else_=99,
        )

        # Wrap query in explicit transaction
        with self.session.begin():
            cases = (
                query.order_by(
                    court_priority,
                    CourtCase.registration_date_ad.desc().nullslast(),
                )
                .limit(self.limit)
                .all()
            )

            # Materialize data inside transaction to avoid lazy loading errors
            result = []
            for case in cases:
                result.append(
                    {
                        "case_number": case.case_number,
                        "court_identifier": case.court_identifier,
                        "registration_date_bs": case.registration_date_bs,
                    }
                )

        self.logger.info(f"Found {len(result)} cases to scrape")
        return result

    def start_requests(self):
        if not self.settings.getbool("ENABLE_CAPTCHA_COOKIE_EXTRACT", False):
            from scrapy.exceptions import CloseSpider

            raise CloseSpider(
                f"[{self.name}] CAPTCHA extraction is DISABLED. "
                "Set ENABLE_CAPTCHA_COOKIE_EXTRACT=True to run this spider."
            )

        cases = self._get_cases_to_scrape()
        self.total_cases = len(cases)

        if not cases:
            self.logger.warning("No cases found to scrape")
            return

        self._pending_cases = deque()
        for case in cases:
            try:
                court_type, court_id = get_court_params(case["court_identifier"])
            except ValueError as e:
                self.logger.error(
                    f"[{case['case_number']}] Skipping — unknown court identifier "
                    f"{case['court_identifier']!r}: {e}"
                )
                # Don't increment failed_cases - this case was never processed
                continue

            self._pending_cases.append(
                {
                    "case_number": case["case_number"],
                    "court_identifier": case["court_identifier"],
                    "court_type": court_type,
                    "court_id": court_id,
                    "registration_date_bs": case["registration_date_bs"] or "",
                }
            )

        self.logger.info(f"Starting to process {self.total_cases} cases")
        yield from self._next_request()

    def _next_request(self):
        """Yield homepage request for next pending case."""
        if not self._pending_cases:
            return

        case_data = self._pending_cases.popleft()
        processed = self.total_cases - len(self._pending_cases)

        self.logger.info(
            f"[{processed}/{self.total_cases}] Processing case {case_data['case_number']} "
            f"(court={case_data['court_identifier']})"
        )

        yield scrapy.Request(
            url="https://supremecourt.gov.np/cp/",
            callback=self.parse,
            meta={**case_data, "handle_httpstatus_list": [301, 302]},
            dont_filter=True,
        )

    def parse(self, response):
        """
        Handle homepage response.

        Redirects are handled manually (REDIRECT_ENABLED=False) because the CAPTCHA
        answer arrives in Set-Cookie on the redirect response. Automatic redirect
        handling would swallow it.
        """
        max_retries = 3
        retry_count = response.meta.get("retry_count", 0)
        redirect_hops = response.meta.get("redirect_hops", 0)
        case_number = response.meta.get("case_number")

        if response.status in (301, 302):
            if redirect_hops >= 10:
                raise ValueError(
                    f"[{case_number}] Too many redirect hops ({redirect_hops})"
                )

            location = response.headers.get("Location")
            if not location:
                raise ValueError(
                    f"[{case_number}] Redirect response missing Location header"
                )

            meta = response.meta.copy()
            meta["redirect_hops"] = redirect_hops + 1

            captcha = self._extract_captcha(response)
            if captcha:
                meta["captcha_solution"] = captcha

            yield scrapy.Request(
                url=urljoin(response.url, location.decode("utf-8")),
                callback=self.parse,
                meta={**meta, "handle_httpstatus_list": [301, 302]},
                dont_filter=True,
            )
            return

        captcha = response.meta.get("captcha_solution") or self._extract_captcha(
            response
        )

        if captcha:
            yield self._submit_form(response, captcha)
            return

        if retry_count < max_retries:
            self.logger.warning(
                f"[{case_number}] No CAPTCHA found, retry {retry_count + 1}/{max_retries}"
            )
            meta = response.meta.copy()
            meta["retry_count"] = retry_count + 1
            meta.pop("captcha_solution", None)
            yield scrapy.Request(
                url="https://supremecourt.gov.np/cp/",
                callback=self.parse,
                meta=meta,
                dont_filter=True,
            )
            return

        raise ValueError(
            f"[{case_number}] Failed to extract CAPTCHA after {max_retries} retries"
        )

    def _extract_captcha(self, response):
        """Extract CAPTCHA answer from PHP session cookie."""
        if not self.settings.getbool("ENABLE_CAPTCHA_COOKIE_EXTRACT", False):
            return None

        for header in response.headers.getlist(b"Set-Cookie"):
            val = header.decode("utf-8", errors="ignore")
            if "court_session=" not in val:
                continue
            match = re.search(r'"captcha_word";s:\d+:"([^"]+)"', unquote(val))
            if match:
                return match.group(1)

        return None

    def _submit_form(self, response, captcha_solution):
        """Submit the search form with case details and extracted CAPTCHA."""
        case_number = response.meta.get("case_number")

        formdata = {
            "court_type": response.meta["court_type"],
            "court_id": response.meta["court_id"],
            "regno": response.meta["case_number"],
            "darta_date": response.meta["registration_date_bs"],
            "faisala_date": "",
            "captcha": captcha_solution,
            "submit": "submit",
        }

        self.logger.info(
            f"[{case_number}] Submitting form: court_type={formdata['court_type']}, "
            f"court_id={formdata['court_id']}, regno={formdata['regno']}, "
            f"darta_date={formdata['darta_date']}"
        )

        # Use FormRequest directly instead of from_response to ensure empty values are sent
        return FormRequest(
            url=response.url,
            formdata=formdata,
            callback=self.parse_results,
            headers={
                "Referer": response.url,
                "Origin": "https://supremecourt.gov.np",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            meta={**response.meta, "handle_httpstatus_list": [301, 302]},
            dont_filter=True,
        )

    def parse_results(self, response):
        """Parse search results page."""
        case_number = response.meta.get("case_number")
        court_identifier = response.meta.get("court_identifier")
        redirect_hops = response.meta.get("redirect_hops", 0)
        captcha_retry_count = response.meta.get("captcha_retry_count", 0)
        max_captcha_retries = 5

        if response.status in (301, 302):
            if redirect_hops >= 10:
                raise ValueError(
                    f"[{case_number}] Too many redirects in results ({redirect_hops})"
                )
            location = response.headers.get("Location")
            if not location:
                raise ValueError(
                    f"[{case_number}] Results redirect missing Location header"
                )
            meta = response.meta.copy()
            meta["redirect_hops"] = redirect_hops + 1
            yield scrapy.Request(
                url=urljoin(response.url, location.decode("utf-8")),
                callback=self.parse_results,
                meta={**meta, "handle_httpstatus_list": [301, 302]},
                dont_filter=True,
            )
            return

        soup = BeautifulSoup(response.text, "html.parser")

        error_table = soup.find("table", bgcolor="#FF6600")
        if error_table:
            error_text = error_table.get_text(strip=True)

            if "Invalid CAPTCHA" in error_text:
                if captcha_retry_count < max_captcha_retries:
                    self.logger.warning(
                        f"[{case_number}] Invalid CAPTCHA, retrying from start "
                        f"({captcha_retry_count + 1}/{max_captcha_retries})"
                    )
                    meta = {
                        "case_number": case_number,
                        "court_identifier": court_identifier,
                        "court_type": response.meta["court_type"],
                        "court_id": response.meta["court_id"],
                        "registration_date_bs": response.meta["registration_date_bs"],
                        "captcha_retry_count": captcha_retry_count + 1,
                        "handle_httpstatus_list": [301, 302],
                    }
                    yield scrapy.Request(
                        url="https://supremecourt.gov.np/cp/",
                        callback=self.parse,
                        meta=meta,
                        dont_filter=True,
                    )
                    return
                else:
                    self.logger.error(
                        f"[{case_number}] Invalid CAPTCHA after {max_captcha_retries} retries. "
                        "Will retry next run."
                    )
                    yield from self._next_request()
                    return
            else:
                self.logger.warning(
                    f"[{case_number}] Server error: {error_text}. Will retry next run."
                )
                yield from self._next_request()
                return

        results_table = soup.find(
            "table", class_="table table-bordered sc-table"
        ) or soup.find("table", class_="table")
        if not results_table:
            # Check if it's a "no records" response
            if "रेकर्ड भेटिएन" in response.text:
                error_msg = "No records found: 'रेकर्ड भेटिएन'"
            else:
                error_msg = "Could not find results table"

            self.logger.warning(f"[{case_number}] {error_msg}.")

            yield {
                "file_urls": [],
                "case_number": case_number,
                "court_identifier": court_identifier,
                "error": error_msg,
            }

            yield from self._next_request()
            return

        tbody = results_table.find("tbody")
        if not tbody:
            error_msg = "Results table has no tbody"
            self.logger.warning(f"[{case_number}] {error_msg}.")

            yield {
                "file_urls": [],
                "case_number": case_number,
                "court_identifier": court_identifier,
                "error": error_msg,
            }

            yield from self._next_request()
            return

        doc_urls = []
        for row in tbody.find_all("tr"):
            cells = row.find_all("td")

            if len(cells) < 10:
                continue

            link = cells[9].find("a", class_="download_content")
            if link and link.get("href"):
                doc_url = urljoin(response.url, link["href"])
                doc_urls.append(doc_url)
                self.logger.info(f"[{case_number}] Found document URL: {doc_url}")

        if not doc_urls:
            error_msg = "Results table found but no download links"
            self.logger.warning(f"[{case_number}] {error_msg}.")

            yield {
                "file_urls": [],
                "case_number": case_number,
                "court_identifier": court_identifier,
                "error": error_msg,
            }

            yield from self._next_request()
            return

        self.logger.info(
            f"[{case_number}] Found {len(doc_urls)} document(s), yielding to pipeline"
        )

        yield {
            "file_urls": doc_urls,
            "case_number": case_number,
            "court_identifier": court_identifier,
        }

        yield from self._next_request()

    def closed(self, reason):
        """Clean up database connections when spider closes."""
        try:
            if hasattr(self, "session") and self.session:
                self.session.close()
            if hasattr(self, "engine") and self.engine:
                self.engine.dispose()
        except Exception as e:
            self.logger.exception(f"Error during spider cleanup: {e}")

        self.logger.info(
            f"Spider closed: {reason} | "
            f"Total: {self.total_cases} | "
            f"Success: {self.successful_cases} | "
            f"Failed: {self.failed_cases}"
        )
