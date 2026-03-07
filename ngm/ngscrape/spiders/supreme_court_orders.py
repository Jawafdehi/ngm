"""
Supreme Court Orders Spider

Scrapes order documents for Special and Supreme Court cases via a CAPTCHA-protected
search form. Yields items to SupremeCourtOrdersPipeline for download and DB update.

Courts: Special (priority 1) → Supreme (priority 2)
"""

import re
import os
import pytz
import random
from collections import deque
from datetime import datetime, timedelta, date
from urllib.parse import urljoin, unquote
from scrapy.http import FormRequest
from bs4 import BeautifulSoup
from sqlalchemy import or_, case as sql_case, and_, func, tuple_

from ngm.database.models import get_engine, get_session, CourtCase, CourtCaseHearing
from ngm.utils.court_mapping import get_court_params
from ngm.ngscrape.pipelines import MIN_DAYS_FOR_DOCUMENTS, TOO_RECENT_RECHECK_DAYS

import scrapy

KATHMANDU_TZ = pytz.timezone("Asia/Kathmandu")
HOMEPAGE_URL = "https://supremecourt.gov.np/cp/"

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]


class SupremeCourtOrdersSpider(scrapy.Spider):
    name = "supreme_court_orders"
    allowed_domains = ("supremecourt.gov.np",)

    custom_settings = {  # noqa: RUF012
        "ITEM_PIPELINES": {"ngm.ngscrape.pipelines.SupremeCourtOrdersPipeline": 1},
        "CONCURRENT_REQUESTS": 1,
        "DOWNLOAD_DELAY": 3,
        "DOWNLOAD_TIMEOUT": 60,
        "COOKIES_ENABLED": True,
        "REDIRECT_ENABLED": False,
        "USER_AGENT": USER_AGENTS[0],
        "DEFAULT_REQUEST_HEADERS": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        },
    }

    MAX_CAPTCHA_RETRIES = 5
    MAX_HOMEPAGE_RETRIES = 3

    def __init__(self, limit=500, *args, **kwargs):
        super().__init__(*args, **kwargs)

        try:
            self.limit = int(limit)
        except (ValueError, TypeError) as e:
            raise ValueError(f"limit must be a valid integer, got: {limit!r}") from e

        if self.limit <= 0:
            raise ValueError(f"limit must be positive, got: {self.limit}")

        db_url = os.getenv("LOCAL_DATABASE_URL") or os.getenv("DATABASE_URL")
        if not db_url:
            raise ValueError(
                "LOCAL_DATABASE_URL or DATABASE_URL environment variable must be set"
            )

        self.engine = get_engine(db_url)
        self.session = get_session(self.engine)
        self.logger.info(
            f"Connected using {'LOCAL_DATABASE_URL' if os.getenv('LOCAL_DATABASE_URL') else 'DATABASE_URL'}"
        )
        self.total_cases = 0
        self.successful_cases = 0
        self.failed_cases = 0

    def _cookiejar_key(self, court_identifier: str, case_number: str) -> str:
        """
        Unique cookie-jar key per (court, case).

        Scrapy reuses one jar per domain by default — the server sees the returning
        court_session cookie and skips re-sending Set-Cookie, so CAPTCHA extraction
        finds nothing. A fresh jar per case forces a new PHP session each time.

        court_identifier is included so the key remains collision-free even if
        the same case_number appears in both "special" and "supreme" courts.
        """
        return f"{court_identifier}_case_{case_number}"

    def _extract_captcha(self, response):
        """
        Extract the CAPTCHA answer from the PHP session cookie in Set-Cookie headers.

        The server embeds the answer in the court_session cookie as a PHP-serialised
        string: a:2:{s:12:"captcha_word";s:5:"hello";...}

        Returns:
            (captcha_word, raw_encoded_cookie_value) or (None, None)
        """
        if not self.settings.getbool("ENABLE_CAPTCHA_COOKIE_EXTRACT", False):
            return None, None

        for raw_header in response.headers.getlist(b"Set-Cookie"):
            header_str = raw_header.decode("utf-8", errors="ignore")
            if "court_session=" not in header_str:
                continue

            value_match = re.search(r"court_session=([^;]+)", header_str)
            if not value_match:
                continue

            raw_cookie_value = value_match.group(1)
            decoded_value = unquote(raw_cookie_value)

            captcha_match = re.search(r'"captcha_word";s:\d+:"([^"]+)"', decoded_value)
            if captcha_match:
                return captcha_match.group(1), raw_cookie_value

        return None, None

    def _is_case_old_enough(self, last_hearing_date, case_number):
        """Return True if last hearing was >= MIN_DAYS_FOR_DOCUMENTS days ago."""
        if not last_hearing_date:
            self.logger.warning(
                f"[{case_number}] No hearing data — treating as too recent (soft-skip)"
            )
            return False

        if isinstance(last_hearing_date, str):
            last_hearing_date = date.fromisoformat(last_hearing_date)

        days_since = (datetime.now(KATHMANDU_TZ).date() - last_hearing_date).days
        is_old_enough = days_since >= MIN_DAYS_FOR_DOCUMENTS
        self.logger.info(
            f"[{case_number}] Last hearing {days_since} days ago -> old_enough={is_old_enough}"
        )
        return is_old_enough

    def _get_cases_to_scrape(self):
        """Query decided Special/Supreme Court cases not yet scraped."""
        query = self.session.query(CourtCase)

        query = query.filter(
            or_(
                CourtCase.court_identifier == "special",
                CourtCase.court_identifier == "supreme",
            )
        )
        query = query.filter(CourtCase.case_status.like("%फैसला%"))
        query = query.filter(
            ~CourtCase.case_status.like("%चालु%"),
            ~CourtCase.case_status.like("%चलिरहेको%"),
        )
        query = query.filter(
            or_(
                CourtCase.extra_data.is_(None),
                CourtCase.extra_data["court_orders"].astext.is_(None),
            )
        )
        query = query.filter(
            or_(
                CourtCase.extra_data.is_(None),
                CourtCase.extra_data["orders_failed"].astext.is_(None),
                CourtCase.extra_data["orders_failed"].astext != "true",
            )
        )

        recheck_cutoff = (
            datetime.now(KATHMANDU_TZ).replace(tzinfo=None)
            - timedelta(days=TOO_RECENT_RECHECK_DAYS)
        ).isoformat()

        query = query.filter(
            or_(
                CourtCase.extra_data.is_(None),
                CourtCase.extra_data["orders_too_recent"].astext.is_(None),
                and_(
                    CourtCase.extra_data["orders_too_recent"].astext == "true",
                    or_(
                        CourtCase.extra_data["orders_too_recent_checked_at"].astext.is_(
                            None
                        ),
                        CourtCase.extra_data["orders_too_recent_checked_at"].astext
                        < recheck_cutoff,
                    ),
                ),
            )
        )

        court_priority = sql_case(
            (CourtCase.court_identifier == "special", 1),
            (CourtCase.court_identifier == "supreme", 2),
            else_=99,
        )

        with self.session.begin():
            cases = (
                query.order_by(
                    court_priority, CourtCase.registration_date_ad.desc().nullslast()
                )
                .limit(self.limit)
                .all()
            )

            case_keys = [(c.case_number, c.court_identifier) for c in cases]

            hearing_dates = {}
            if case_keys:
                rows = (
                    self.session.query(
                        CourtCaseHearing.case_number,
                        CourtCaseHearing.court_identifier,
                        func.max(CourtCaseHearing.hearing_date_ad).label(
                            "last_hearing_date"
                        ),
                    )
                    .filter(
                        tuple_(
                            CourtCaseHearing.case_number,
                            CourtCaseHearing.court_identifier,
                        ).in_(case_keys)
                    )
                    .group_by(
                        CourtCaseHearing.case_number, CourtCaseHearing.court_identifier
                    )
                    .all()
                )
                hearing_dates = {
                    (r.case_number, r.court_identifier): r.last_hearing_date
                    for r in rows
                }

            result = [
                {
                    "case_number": c.case_number,
                    "court_identifier": c.court_identifier,
                    "registration_date_bs": c.registration_date_bs,
                    "last_hearing_date": hearing_dates.get(
                        (c.case_number, c.court_identifier)
                    ),
                }
                for c in cases
            ]

        self.logger.info(f"Found {len(result)} cases to scrape")
        return result

    def start_requests(self):
        if not self.settings.getbool("ENABLE_CAPTCHA_COOKIE_EXTRACT", False):
            from scrapy.exceptions import CloseSpider

            raise CloseSpider(
                f"[{self.name}] CAPTCHA extraction is DISABLED. "
                "Set ENABLE_CAPTCHA_COOKIE_EXTRACT=True to run this spider."
            )

        try:
            cases = self._get_cases_to_scrape()
        except Exception:
            self.logger.exception("Failed to query cases — aborting spider")
            return

        if not cases:
            self.logger.warning("No cases found to scrape")
            return

        self._pending_cases = deque()
        for case in cases:
            try:
                court_type, court_id = get_court_params(case["court_identifier"])
            except ValueError as e:
                self.logger.error(
                    f"[{case['case_number']}] Skipping — unknown court {case['court_identifier']!r}: {e}"
                )
                continue

            self._pending_cases.append(
                {
                    "case_number": case["case_number"],
                    "court_identifier": case["court_identifier"],
                    "court_type": court_type,
                    "court_id": court_id,
                    "registration_date_bs": case["registration_date_bs"] or "",
                    "last_hearing_date": case.get("last_hearing_date"),
                }
            )

        self.total_cases = len(self._pending_cases)
        self.logger.info(f"Starting to process {self.total_cases} cases")
        yield from self._next_request()

    def _next_request(self):
        """Yield a homepage GET for the next pending case."""
        if not self._pending_cases:
            return

        case_data = self._pending_cases.popleft()
        case_number = case_data["case_number"]
        processed = self.total_cases - len(self._pending_cases)

        self.logger.info(
            f"[{processed}/{self.total_cases}] Processing case {case_number} "
            f"(court={case_data['court_identifier']})"
        )

        ua = random.choice(USER_AGENTS)  # noqa: S311
        yield scrapy.Request(
            url=HOMEPAGE_URL,
            callback=self.parse,
            headers={"User-Agent": ua},
            meta={
                **case_data,
                "cookiejar": self._cookiejar_key(
                    case_data["court_identifier"], case_number
                ),
                "handle_httpstatus_list": [301, 302],
                "user_agent": ua,
            },
            dont_filter=True,
        )

    def parse(self, response):
        """Handle homepage response and extract CAPTCHA."""
        retry_count = response.meta.get("retry_count", 0)
        redirect_hops = response.meta.get("redirect_hops", 0)
        case_number = response.meta.get("case_number")

        try:
            yield from self._do_parse(response, retry_count, redirect_hops, case_number)
        except Exception:
            self.failed_cases += 1
            self.logger.exception(f"[{case_number}] Unexpected error in parse")
            yield from self._next_request()

    def _do_parse(self, response, retry_count, redirect_hops, case_number):
        """Inner parse logic — wrapped by parse() to keep _pending_cases draining on error."""
        if response.status in (301, 302):
            if redirect_hops >= 10:
                self.logger.error(f"[{case_number}] Too many redirect hops. Skipping.")
                yield from self._next_request()
                return

            location = response.headers.get("Location")
            if not location:
                self.logger.error(
                    f"[{case_number}] Redirect missing Location header. Skipping."
                )
                yield from self._next_request()
                return

            meta = response.meta.copy()
            meta["redirect_hops"] = redirect_hops + 1
            captcha, raw_cookie = self._extract_captcha(response)
            if captcha:
                meta["captcha_solution"] = captcha
                meta["court_session_cookie"] = raw_cookie

            yield scrapy.Request(
                url=urljoin(response.url, location.decode("utf-8")),
                callback=self.parse,
                headers={
                    "User-Agent": meta.get("user_agent", self.settings["USER_AGENT"])
                },
                meta={**meta, "handle_httpstatus_list": [301, 302]},
                dont_filter=True,
            )
            return

        fresh_captcha, fresh_cookie = self._extract_captcha(response)
        captcha = response.meta.get("captcha_solution") or fresh_captcha
        raw_cookie = response.meta.get("court_session_cookie") or fresh_cookie

        if captcha:
            self.logger.info(
                f"[{case_number}] Extracted CAPTCHA (length={len(captcha)})"
            )
            yield self._submit_form(response, captcha, raw_cookie)
            return

        if retry_count < self.MAX_HOMEPAGE_RETRIES:
            self.logger.warning(
                f"[{case_number}] No CAPTCHA found (retry {retry_count + 1}/{self.MAX_HOMEPAGE_RETRIES})"
            )
            meta = response.meta.copy()
            meta["retry_count"] = retry_count + 1
            meta.pop("captcha_solution", None)
            meta.pop("court_session_cookie", None)
            yield scrapy.Request(
                url=HOMEPAGE_URL,
                callback=self.parse,
                headers={
                    "User-Agent": meta.get("user_agent", self.settings["USER_AGENT"])
                },
                meta=meta,
                dont_filter=True,
            )
            return

        self.logger.error(
            f"[{case_number}] Failed to extract CAPTCHA after {self.MAX_HOMEPAGE_RETRIES} retries. "
            "Skipping — will retry next run."
        )
        yield from self._next_request()

    def _submit_form(self, response, captcha_solution, court_session_cookie=None):
        """POST the search form with CAPTCHA and injected session cookie."""
        case_number = response.meta.get("case_number")

        formdata = {
            "court_type": response.meta["court_type"],
            "court_id": response.meta["court_id"],
            "regno": case_number,
            "darta_date": response.meta["registration_date_bs"],
            "faisala_date": "",
            "captcha": captcha_solution,
            "submit": "submit",
        }

        self.logger.info(
            f"[{case_number}] Submitting form: court_type={formdata['court_type']}, "
            f"court_id={formdata['court_id']}, "
            f"manual_cookie={'SET' if court_session_cookie else 'MISSING'}"
        )

        headers = {
            "Referer": response.url,
            "Origin": "https://supremecourt.gov.np",
            "User-Agent": response.meta.get("user_agent", self.settings["USER_AGENT"]),
        }
        if court_session_cookie:
            headers["Cookie"] = f"court_session={court_session_cookie}"

        meta = response.meta.copy()
        meta["submitted_captcha_length"] = len(captcha_solution)

        return FormRequest(
            url=response.url,
            formdata=formdata,
            callback=self.parse_results,
            headers=headers,
            meta={**meta, "handle_httpstatus_list": [301, 302]},
            dont_filter=True,
        )

    def parse_results(self, response):
        """Parse search results and extract document URLs."""
        case_number = response.meta.get("case_number")
        court_identifier = response.meta.get("court_identifier")
        redirect_hops = response.meta.get("redirect_hops", 0)
        captcha_retry_count = response.meta.get("captcha_retry_count", 0)

        try:
            yield from self._do_parse_results(
                response,
                case_number,
                court_identifier,
                redirect_hops,
                captcha_retry_count,
            )
        except Exception:
            self.failed_cases += 1
            self.logger.exception(f"[{case_number}] Unexpected error in parse_results")
            yield from self._next_request()

    def _do_parse_results(
        self,
        response,
        case_number,
        court_identifier,
        redirect_hops,
        captcha_retry_count,
    ):
        """Inner parse_results logic — wrapped by parse_results() to keep queue draining on error."""
        if response.status in (301, 302):
            if redirect_hops >= 10:
                self.logger.error(f"[{case_number}] Too many redirects. Skipping.")
                yield from self._next_request()
                return

            location = response.headers.get("Location")
            if not location:
                self.logger.error(
                    f"[{case_number}] Results redirect missing Location. Skipping."
                )
                yield from self._next_request()
                return

            meta = response.meta.copy()
            meta["redirect_hops"] = redirect_hops + 1
            yield scrapy.Request(
                url=urljoin(response.url, location.decode("utf-8")),
                callback=self.parse_results,
                headers={
                    "User-Agent": meta.get("user_agent", self.settings["USER_AGENT"])
                },
                meta={**meta, "handle_httpstatus_list": [301, 302]},
                dont_filter=True,
            )
            return

        soup = BeautifulSoup(response.text, "html.parser")

        error_table = soup.find("table", bgcolor="#FF6600")
        if error_table:
            error_text = error_table.get_text(strip=True)
            if "Invalid CAPTCHA" in error_text:
                self.logger.error(f"[{case_number}] Invalid CAPTCHA")
                if captcha_retry_count < self.MAX_CAPTCHA_RETRIES:
                    self.logger.warning(
                        f"[{case_number}] Retrying with fresh session "
                        f"({captcha_retry_count + 1}/{self.MAX_CAPTCHA_RETRIES})"
                    )
                    yield scrapy.Request(
                        url=HOMEPAGE_URL,
                        callback=self.parse,
                        headers={
                            "User-Agent": response.meta.get(
                                "user_agent", self.settings["USER_AGENT"]
                            )
                        },
                        meta={
                            "case_number": case_number,
                            "court_identifier": court_identifier,
                            "court_type": response.meta["court_type"],
                            "court_id": response.meta["court_id"],
                            "registration_date_bs": response.meta[
                                "registration_date_bs"
                            ],
                            "last_hearing_date": response.meta.get("last_hearing_date"),
                            "captcha_retry_count": captcha_retry_count + 1,
                            "handle_httpstatus_list": [301, 302],
                            "user_agent": response.meta.get("user_agent"),
                            "cookiejar": f"{self._cookiejar_key(court_identifier, case_number)}_retry{captcha_retry_count + 1}",
                        },
                        dont_filter=True,
                    )
                    return
                self.logger.error(
                    f"[{case_number}] Invalid CAPTCHA after {self.MAX_CAPTCHA_RETRIES} retries. "
                    "Will retry next run."
                )
            else:
                self.logger.warning(
                    f"[{case_number}] Server error: {error_text}. Will retry next run."
                )
            yield from self._next_request()
            return

        if "फैसला / आदेश को पुर्ण पाठ" not in response.text:
            self.logger.warning(
                f"[{case_number}] Not a valid results page. Will retry next run."
            )
            yield from self._next_request()
            return

        if "रेकर्ड भेटिएन" in response.text:
            self.logger.warning(
                f"[{case_number}] No records found. Will retry next run."
            )
            yield from self._next_request()
            return

        results_table = soup.find(
            "table", class_="table table-bordered sc-table"
        ) or soup.find("table", class_="table")
        if not results_table:
            self.logger.warning(
                f"[{case_number}] Results table missing. Will retry next run."
            )
            yield from self._next_request()
            return

        tbody = results_table.find("tbody")
        if not tbody:
            self.logger.warning(
                f"[{case_number}] Results table has no tbody. Will retry next run."
            )
            yield from self._next_request()
            return

        doc_urls = []
        seen = set()
        for row in tbody.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 10:
                continue
            link = cells[9].find("a", class_="download_content")
            if link and link.get("href"):
                doc_url = urljoin(response.url, link["href"])
                if doc_url not in seen:
                    seen.add(doc_url)
                    doc_urls.append(doc_url)
                    self.logger.info(f"[{case_number}] Found document URL: {doc_url}")

        if not doc_urls:
            is_old_enough = self._is_case_old_enough(
                response.meta.get("last_hearing_date"), case_number
            )
            error = "no_docs_old_case" if is_old_enough else "too_recent"
            if is_old_enough:
                self.logger.warning(
                    f"[{case_number}] Old case — marking as permanent failure."
                )
            else:
                self.logger.info(
                    f"[{case_number}] Recent case — will recheck in {TOO_RECENT_RECHECK_DAYS} days."
                )
            yield {
                "file_urls": [],
                "case_number": case_number,
                "court_identifier": court_identifier,
                "error": error,
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
        if hasattr(self, "session") and self.session:
            try:
                self.session.close()
            except Exception:
                self.logger.exception("Error closing database session")

        if hasattr(self, "engine") and self.engine:
            try:
                self.engine.dispose()
            except Exception:
                self.logger.exception("Error disposing database engine")

        self.logger.info(
            f"Spider closed: {reason} | "
            f"Total: {self.total_cases} | "
            f"Success: {self.successful_cases} | Failed: {self.failed_cases}"
        )
