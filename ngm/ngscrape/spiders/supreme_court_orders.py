"""
Supreme Court Orders Spider (Listing Only)

Scrapes basic order information from Supreme Court website.
This spider only lists cases with orders - it does NOT download documents.
Use supreme_orders_enrichment.py to download the actual order documents.

Target: https://supremecourt.gov.np/cp/
"""

import scrapy
import re
import os
from urllib.parse import urljoin, urlparse, unquote
from datetime import datetime
from scrapy.http import FormRequest
from bs4 import BeautifulSoup
from sqlalchemy.orm.attributes import flag_modified

from ngm.database.models import get_engine, get_session, CourtCase, Court
from ngm.utils.court_mapping import get_court_params
from sqlalchemy import or_, and_, case as sql_case


class SupremeCourtOrdersSpider(scrapy.Spider):
    name = "supreme_court_orders"
    allowed_domains = ["supremecourt.gov.np"]

    custom_settings = {
        "CONCURRENT_REQUESTS": 1,
        "DOWNLOAD_DELAY": 3,  # delay between requests
        "DOWNLOAD_TIMEOUT": 60,
        "COOKIES_ENABLED": True,
        "REDIRECT_ENABLED": False,  # Handle redirects manually in parse() method
        "USER_AGENT": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "DEFAULT_REQUEST_HEADERS": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        },
    }

    def __init__(self, limit=5, *args, **kwargs):
        """
        Initialize spider with database connection.

        Args:
            limit: Maximum number of cases to process (default: 5)
        """
        super().__init__(*args, **kwargs)

        self.limit = int(limit)

        # Use LOCAL_DATABASE_URL for local testing, fallback to DATABASE_URL for production
        db_url = os.getenv("LOCAL_DATABASE_URL") or os.getenv("DATABASE_URL")

        if not db_url:
            raise ValueError("Neither LOCAL_DATABASE_URL nor DATABASE_URL is set")

        # Print which database we're using
        db_type = "LOCAL" if os.getenv("LOCAL_DATABASE_URL") else "PRODUCTION"
        print(f"\n{'='*60}")
        parsed = urlparse(db_url)
        safe_url = f"{parsed.scheme}://****:****@{parsed.hostname}{parsed.path}"
        print(f"Using {db_type} database")
        print(f"DATABASE_URL: {safe_url}")
        print(f"{'='*60}\n")

        self.engine = get_engine(db_url)
        self.session = get_session(self.engine)

        self.total_cases = 0
        self.successful_cases = 0
        self.failed_cases = 0

        self.logger.info(f"Spider initialized (limit: {self.limit} cases)")

    def start(self):
        return super().start()

    def _get_cases_to_scrape(self):
        """
        Query database for cases that need order documents.
        Returns cases with final decisions that haven't been scraped yet.
        """
        try:
            query = self.session.query(CourtCase).join(Court)

            # Filter: Has final decision
            has_decision = or_(
                CourtCase.verdict_date_ad.isnot(None),
                and_(
                    CourtCase.verdict_date_bs.isnot(None),
                    CourtCase.verdict_date_bs != "**** ** **",
                ),
                CourtCase.case_status.like("%फैसला%"),
            )
            query = query.filter(has_decision)

            # Filter: Not ongoing
            query = query.filter(
                or_(
                    CourtCase.case_status.is_(None),
                    ~CourtCase.case_status.like("%चालु%"),
                )
            )

            # Filter: Not already scraped
            query = query.filter(
                or_(
                    CourtCase.extra_data.is_(None),
                    CourtCase.extra_data["orders_scraped"].astext.is_(None),
                    CourtCase.extra_data["orders_scraped"].astext != "true",
                )
            )

            # Priority ordering: Special → Supreme → High → District
            court_priority = sql_case(
                (Court.court_type == "special", 1),
                (Court.court_type == "supreme", 2),
                (Court.court_type == "high", 3),
                (Court.court_type == "district", 4),
                else_=5,
            )

            query = query.order_by(
                court_priority, CourtCase.registration_date_ad.asc().nullslast()
            )

            query = query.limit(self.limit)

            cases = query.all()
            self.logger.info(f"Found {len(cases)} cases to scrape")
            return cases

        except Exception as e:
            self.logger.error(f"Error querying cases: {e}")
            return []

    def start_requests(self):
        with self.session.begin():
            cases = self._get_cases_to_scrape()
            self.total_cases = len(cases)

            if not cases:
                self.logger.warning("No cases found to scrape")
                return

            # Extract plain data INSIDE the transaction while session is active
            self._pending_cases = []
            for case in cases:
                try:
                    court_type, court_id = get_court_params(case.court_identifier)
                    self._pending_cases.append(
                        {
                            "case_number": case.case_number,
                            "court_identifier": case.court_identifier,
                            "court_type": court_type,
                            "court_id": court_id,
                            "registration_date_bs": case.registration_date_bs or "",
                        }
                    )
                except Exception as e:
                    self.logger.error(f"Error preparing case {case.case_number}: {e}")
                    self.failed_cases += 1

        self.logger.info(f"Starting to process {self.total_cases} cases")
        yield from self._yield_next_case()

    # produces a Scrapy Request for each case
    def _yield_next_case(self):
        """Yield request for the next pending case."""
        if not self._pending_cases:
            return

        case_data = self._pending_cases.pop(0)  # plain dict, no ORM
        yield scrapy.Request(
            url="https://supremecourt.gov.np/cp/",
            callback=self.parse,
            meta={  # meta passes extra info
                **case_data,
                "handle_httpstatus_list": [302],
            },
            dont_filter=True,  # Scrapy filters duplicate URLS,  but we may need to retry same URL
        )

    def parse(self, response):
        """Extract CAPTCHA and submit search form, handling redirects manually."""
        max_retries = 3
        retry_count = response.meta.get("retry_count", 0)

        try:
            # Handle redirect manually so we can capture the CAPTCHA cookie
            # before the server refreshes the session on redirect
            if response.status in (301, 302):
                redirect_url = response.headers.get("Location", b"").decode("utf-8")
                redirect_url = urljoin(response.url, redirect_url)

                captcha_from_redirect = self._extract_captcha_from_session_cookie(
                    response
                )

                meta = response.meta.copy()
                if captcha_from_redirect:
                    meta["captcha_solution"] = captcha_from_redirect

                yield scrapy.Request(
                    url=redirect_url,
                    callback=self.parse,
                    meta={
                        **meta,
                        "handle_httpstatus_list": [302],
                    },
                    dont_filter=True,
                )
                return

            # Check if CAPTCHA was already captured during redirect
            captcha_solution = response.meta.get("captcha_solution")

            # If not, try to extract from current response cookies
            if not captcha_solution:
                captcha_solution = self._extract_captcha_from_session_cookie(response)

            if captcha_solution:
                yield self.submit_search_form(response, captcha_solution)
            else:
                if retry_count < max_retries:
                    self.logger.warning(
                        f"Failed to extract CAPTCHA, retry {retry_count + 1}/{max_retries}"
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
                else:
                    case_number = response.meta.get("case_number")
                    court_identifier = response.meta.get("court_identifier")
                    self.logger.error(
                        f"Failed to extract CAPTCHA after {max_retries} attempts for case {case_number}"
                    )
                    self.failed_cases += 1
                    self._mark_case_failed(
                        case_number=case_number,
                        court_identifier=court_identifier,
                        error_message=f"Failed to extract CAPTCHA after {max_retries} attempts",
                    )
                    yield from self._yield_next_case()

        except Exception as e:
            self.logger.error(f"Error in parse: {e}")
            self.failed_cases += 1
            case_number = response.meta.get("case_number")
            court_identifier = response.meta.get("court_identifier")
            self._mark_case_failed(
                case_number=case_number,
                court_identifier=court_identifier,
                error_message=str(e),
            )
            yield from self._yield_next_case()  # dont stall the queue

    def _extract_captcha_from_session_cookie(self, response):
        """
        Extract CAPTCHA answer from leaked session cookie.

        WARNING: This feature is gated behind ENABLE_CAPTCHA_COOKIE_EXTRACT setting.
        Requires documented legal/compliance approval before enabling in production.
        """
        # Check if the feature is enabled via settings
        if not self.settings.getbool("ENABLE_CAPTCHA_COOKIE_EXTRACT", False):
            self.logger.debug(
                "CAPTCHA cookie extraction is disabled. "
                "Set ENABLE_CAPTCHA_COOKIE_EXTRACT=True in settings after obtaining "
                "legal/compliance approval to enable this feature."
            )
            return None

        self.logger.warning(
            "CAPTCHA cookie extraction is ENABLED. "
            "Ensure legal/compliance approval is documented before using in production."
        )

        for header in response.headers.getlist(b"Set-Cookie"):
            val = header.decode("utf-8", errors="ignore")

            if "court_session=" not in val:
                continue

            unquoted = unquote(val)
            match = re.search(r'"captcha_word";s:\d+:"([^"]+)"', unquoted)
            if match:
                return match.group(1)

        return None

    def submit_search_form(self, response, captcha_solution):
        """Submit the search form with case details and CAPTCHA."""
        court_type = response.meta["court_type"]
        court_id = response.meta["court_id"]
        case_no = response.meta["case_number"]
        registration_date = response.meta["registration_date_bs"]

        formdata = {
            "court_type": court_type,
            "court_id": court_id,
            "regno": case_no,
            "darta_date": registration_date,
            "faisala_date": "",
            "captcha": captcha_solution,
            "submit": "submit",
        }

        return FormRequest.from_response(
            response,
            formdata=formdata,
            callback=self.parse_search_results,
            headers={
                "Referer": response.url,
                "Origin": "https://supremecourt.gov.np",
            },
            meta={
                **response.meta,
                "handle_httpstatus_list": [302],
            },
            dont_filter=True,
        )

    def parse_search_results(self, response):
        """Parse search results and save order information to database."""
        case_number = response.meta.get("case_number")

        if response.status == 302:
            redirect_url = response.headers.get("Location", b"").decode("utf-8")
            redirect_url = urljoin(response.url, redirect_url)
            self.logger.info(f"[{case_number}] POST redirected to: {redirect_url}")
            yield scrapy.Request(
                url=redirect_url,
                callback=self.parse_search_results,
                meta={
                    **response.meta,
                    "handle_httpstatus_list": [302],
                },
                dont_filter=True,
            )
            return

        try:
            soup = BeautifulSoup(response.text, "html.parser")

            # Check for errors
            error_table = soup.find("table", bgcolor="#FF6600")
            if error_table:
                error_text = error_table.get_text(strip=True)
                self.logger.error(f"[{case_number}] Server error: {error_text}")
                self.failed_cases += 1
                return

            # Check for no results
            if "कुनै रेकर्ड भेटिएन" in response.text:
                self.logger.warning(f"[{case_number}] No results found")
                self.failed_cases += 1
                return

            # Find results table
            results_table = soup.find("table", class_="table table-bordered sc-table")
            if not results_table:
                results_table = soup.find("table", class_="table")

            if not results_table:
                self.logger.error(f"[{case_number}] Could not find results table")
                self.failed_cases += 1
                return

            tbody = results_table.find("tbody")
            if not tbody:
                self.logger.error(f"[{case_number}] No tbody found in results table")
                self.failed_cases += 1
                return

            rows = tbody.find_all("tr")
            self.logger.info(f"[{case_number}] Found {len(rows)} row(s)")

            for idx, row in enumerate(rows, 1):
                cells = row.find_all("td")

                if len(cells) < 10:
                    self.logger.warning(
                        f"[{case_number}] Row {idx}: Unexpected format ({len(cells)} cells)"
                    )
                    continue

                doc_link = cells[9].find("a", class_="download_content")

                if doc_link and doc_link.get("href"):
                    doc_url = urljoin(response.url, doc_link["href"])
                    self.logger.info(f"[{case_number}] Document URL: {doc_url}")

                    self._save_order_info(
                        case_number=response.meta["case_number"],
                        court_identifier=response.meta["court_identifier"],
                        document_url=doc_url,
                        enrichment_data={
                            "registration_number": cells[1].get_text(strip=True),
                            "case_number_from_site": cells[2].get_text(strip=True),
                            "decision_date": cells[8].get_text(strip=True),
                        },
                    )

                    self.successful_cases += 1
                else:
                    self.logger.warning(f"[{case_number}] Row {idx}: No download link")
                    self.failed_cases += 1

        except Exception as e:
            self.logger.error(f"[{case_number}] Exception: {e}", exc_info=True)
            self.failed_cases += 1

        yield from self._yield_next_case()

    def _save_order_info(
        self, case_number, court_identifier, document_url, enrichment_data=None
    ):
        """Save order document URL and enrichment data to database."""
        try:
            with self.session.begin():
                case = (
                    self.session.query(CourtCase)
                    .filter_by(
                        case_number=case_number, court_identifier=court_identifier
                    )
                    .first()
                )

                if case:
                    if case.extra_data is None:
                        case.extra_data = {}

                    case.extra_data["order_document_url"] = document_url
                    case.extra_data["order_found_at"] = datetime.now().isoformat()

                    # Store all table-cell data from the website
                    if enrichment_data:
                        case.extra_data["enrichment_registration_number"] = (
                            enrichment_data.get("registration_number")
                        )
                        case.extra_data["enrichment_case_number"] = enrichment_data.get(
                            "case_number_from_site"
                        )
                        case.extra_data["enrichment_decision_date"] = (
                            enrichment_data.get("decision_date")
                        )

                    case.status = "pending"
                    flag_modified(case, "extra_data")
                    self.logger.info(f"Saved order info for case {case_number}")
                else:
                    self.logger.warning(f"Case not found: {case_number}")

        except Exception as e:
            self.logger.error(f"Error saving order info: {e}")

    def _mark_case_failed(self, case_number, court_identifier, error_message):
        """Mark a case as failed in the database."""
        try:
            with self.session.begin():
                case = (
                    self.session.query(CourtCase)
                    .filter_by(
                        case_number=case_number, court_identifier=court_identifier
                    )
                    .first()
                )

                if case:
                    if case.extra_data is None:
                        case.extra_data = {}

                    case.extra_data["order_listing_failed"] = True
                    case.extra_data["order_listing_error"] = error_message
                    case.extra_data["order_listing_failed_at"] = (
                        datetime.now().isoformat()
                    )
                    flag_modified(case, "extra_data")

        except Exception as e:
            self.logger.error(f"Error marking case as failed: {e}")

    def closed(self, reason):
        """Spider cleanup and summary logging."""
        try:
            if hasattr(self, "session") and self.session:
                self.session.close()

            if hasattr(self, "engine") and self.engine:
                self.engine.dispose()

            self.logger.info("=" * 60)
            self.logger.info(
                f"Total: {self.total_cases} | Success: {self.successful_cases} | Failed: {self.failed_cases}"
            )
            self.logger.info("=" * 60)

        except Exception as e:
            self.logger.error(f"Error in cleanup: {e}")
