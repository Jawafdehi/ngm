"""
Supreme Court Orders Spider (Listing Only)

Scrapes basic order information from Supreme Court website.
This spider only lists cases with orders - it does NOT download documents.
Use supreme_orders_enrichment.py to download the actual order documents.
"""

import scrapy
import re
import os
import pytz
from collections import deque
from urllib.parse import urljoin, urlparse, unquote
from datetime import datetime
from scrapy.http import FormRequest
from bs4 import BeautifulSoup
from sqlalchemy.orm.attributes import flag_modified

from ngm.database.models import get_engine, get_session, CourtCase, Court
from ngm.utils.court_mapping import get_court_params
from sqlalchemy import or_, and_, case as sql_case

KATHMANDU_TZ = pytz.timezone("Asia/Kathmandu")


class SupremeCourtOrdersSpider(scrapy.Spider):
    name = "supreme_court_orders"
    allowed_domains = ("supremecourt.gov.np",)

    custom_settings = {  # noqa: RUF012
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

    def __init__(self, limit=500, *args, **kwargs):
        super().__init__(*args, **kwargs)

        try:
            self.limit = int(limit)
        except (ValueError, TypeError) as e:
            raise ValueError(f"limit must be a valid integer, got: {limit}") from e

        if self.limit <= 0:
            raise ValueError(f"limit must be positive, got: {self.limit}")

        db_url = os.getenv("LOCAL_DATABASE_URL") or os.getenv("DATABASE_URL")
        if not db_url:
            raise ValueError("LOCAL_DATABASE_URL or DATABASE_URL not set")

        # Log database type
        db_type = "LOCAL" if os.getenv("LOCAL_DATABASE_URL") else "PROD"
        parsed = urlparse(db_url)

        # Build safe URL with proper masking
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        query = f"?{parsed.query}" if parsed.query else ""

        # Only include credentials if they exist
        if parsed.username or parsed.password:
            safe_url = f"{parsed.scheme}://****:****@{host}{port}{parsed.path}{query}"
        else:
            safe_url = f"{parsed.scheme}://{host}{port}{parsed.path}{query}"

        self.logger.info(f"Using {db_type} database: {safe_url}")

        self.engine = get_engine(db_url)
        self.session = get_session(self.engine)

        self.total_cases = 0
        self.successful_cases = 0
        self.failed_cases = 0

    def _get_cases_to_scrape(self):
        """Query cases with final decisions that haven't been scraped."""
        try:
            query = self.session.query(CourtCase).join(Court)

            # Has final decision
            has_decision = or_(
                CourtCase.verdict_date_ad.isnot(None),
                and_(
                    CourtCase.verdict_date_bs.isnot(None),
                    CourtCase.verdict_date_bs != "**** ** **",
                ),
                CourtCase.case_status.like("%फैसला%"),
            )
            query = query.filter(has_decision)

            # Not ongoing
            query = query.filter(
                or_(
                    CourtCase.case_status.is_(None),
                    ~CourtCase.case_status.like("%चालु%"),
                )
            )

            # Not already scraped
            query = query.filter(
                or_(
                    CourtCase.extra_data.is_(None),
                    and_(
                        or_(
                            CourtCase.extra_data["orders_scraped"].astext.is_(None),
                            CourtCase.extra_data["orders_scraped"].astext != "true",
                        ),
                        or_(
                            CourtCase.extra_data["order_document_url"].astext.is_(None),
                            CourtCase.extra_data["order_document_url"].astext == "",
                        ),
                    ),
                )
            )

            # Priority: Special → Supreme → High → District
            court_priority = sql_case(
                (Court.court_type == "special", 1),
                (Court.court_type == "supreme", 2),
                (Court.court_type == "high", 3),
                (Court.court_type == "district", 4),
                else_=5,
            )

            cases = (
                query.order_by(
                    court_priority, CourtCase.registration_date_ad.asc().nullslast()
                )
                .limit(self.limit)
                .all()
            )

            self.logger.info(f"Found {len(cases)} cases to scrape")
            return cases

        except Exception:
            self.logger.exception("Error querying cases")
            return []

    def start_requests(self):
        # Check CAPTCHA extraction setting early - fail fast if disabled
        if not self.settings.getbool("ENABLE_CAPTCHA_COOKIE_EXTRACT", False):
            from scrapy.exceptions import CloseSpider

            raise CloseSpider(
                f"[{self.name}] CAPTCHA cookie extraction is DISABLED. "
                "Spider cannot function without CAPTCHA solving. "
                "Set ENABLE_CAPTCHA_COOKIE_EXTRACT=True after obtaining legal/compliance approval."
            )

        with self.session.begin():
            cases = self._get_cases_to_scrape()
            self.total_cases = len(cases)

            if not cases:
                self.logger.warning("No cases found to scrape")
                return

            # Use deque instead of list for O(1) popleft operations
            self._pending_cases = deque()
            failed_preparations = []

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
                except ValueError as e:
                    self.logger.exception(f"Error preparing case {case.case_number}")
                    self.failed_cases += 1
                    failed_preparations.append(
                        {
                            "case_number": case.case_number,
                            "court_identifier": case.court_identifier,
                            "error_message": f"Error preparing case: {e}",
                        }
                    )

        # Mark failures outside the transaction to avoid nested Session.begin()
        for failure in failed_preparations:
            self._mark_case_failed(**failure)

        self.logger.info(f"Starting to process {self.total_cases} cases")
        yield from self._yield_next_case()

    def _yield_next_case(self):
        """Yield request for next pending case."""
        if not self._pending_cases:
            return

        case_data = self._pending_cases.popleft()
        yield scrapy.Request(
            url="https://supremecourt.gov.np/cp/",
            callback=self.parse,
            meta={**case_data, "handle_httpstatus_list": [301, 302]},
            dont_filter=True,
        )

    def parse(self, response):
        """Extract CAPTCHA and submit search form, handling redirects manually."""
        max_retries = 3
        retry_count = response.meta.get("retry_count", 0)
        max_redirect_hops = 10
        redirect_hops = response.meta.get("redirect_hops", 0)

        try:
            # Handle redirect manually
            if response.status in (301, 302):
                # Check redirect hop limit
                if redirect_hops >= max_redirect_hops:
                    self.logger.error(
                        f"[{response.meta.get('case_number')}] Too many redirects ({redirect_hops} hops)"
                    )
                    self.failed_cases += 1
                    self._mark_case_failed(
                        case_number=response.meta.get("case_number"),
                        court_identifier=response.meta.get("court_identifier"),
                        error_message=f"Too many redirect hops ({redirect_hops})",
                    )
                    yield from self._yield_next_case()
                    return

                location = response.headers.get("Location")

                # Guard against missing/empty Location header
                if not location:
                    self.logger.warning(
                        f"Redirect response with empty Location header for case {response.meta.get('case_number')}"
                    )
                    self.failed_cases += 1
                    self._mark_case_failed(
                        case_number=response.meta.get("case_number"),
                        court_identifier=response.meta.get("court_identifier"),
                        error_message="Redirect with empty Location header",
                    )
                    yield from self._yield_next_case()
                    return

                redirect_url = urljoin(response.url, location.decode("utf-8"))

                captcha_from_redirect = self._extract_captcha_from_session_cookie(
                    response
                )

                meta = response.meta.copy()
                if captcha_from_redirect:
                    meta["captcha_solution"] = captcha_from_redirect
                meta["redirect_hops"] = redirect_hops + 1

                yield scrapy.Request(
                    url=redirect_url,
                    callback=self.parse,
                    meta={
                        **meta,
                        "handle_httpstatus_list": [301, 302],
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
            # Use logger.exception to preserve traceback
            self.logger.exception("Error in parse")
            self.failed_cases += 1
            case_number = response.meta.get("case_number")
            court_identifier = response.meta.get("court_identifier")
            self._mark_case_failed(
                case_number=case_number,
                court_identifier=court_identifier,
                error_message=str(e),
            )
            yield from self._yield_next_case()

    def _extract_captcha_from_session_cookie(self, response):
        """Extract CAPTCHA from session cookie (requires ENABLE_CAPTCHA_COOKIE_EXTRACT=True)."""
        if not self.settings.getbool("ENABLE_CAPTCHA_COOKIE_EXTRACT", False):
            return None

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
                "handle_httpstatus_list": [301, 302],
            },
            dont_filter=True,
        )

    def parse_search_results(self, response):
        """Parse search results and save order information to database."""
        case_number = response.meta.get("case_number")
        court_identifier = response.meta.get("court_identifier")
        max_redirect_hops = 10
        redirect_hops = response.meta.get("redirect_hops", 0)

        if response.status in (301, 302):
            # Check redirect hop limit
            if redirect_hops >= max_redirect_hops:
                self.logger.error(
                    f"[{case_number}] Too many redirects in search results ({redirect_hops} hops)"
                )
                self.failed_cases += 1
                self._mark_case_failed(
                    case_number=case_number,
                    court_identifier=court_identifier,
                    error_message=f"Too many redirect hops in search results ({redirect_hops})",
                )
                yield from self._yield_next_case()
                return

            location = response.headers.get("Location")

            # Guard against missing/empty Location header
            if not location:
                self.logger.warning(f"[{case_number}] Redirect without Location header")
                self.failed_cases += 1
                self._mark_case_failed(
                    case_number=case_number,
                    court_identifier=court_identifier,
                    error_message="Redirect without Location header",
                )
                yield from self._yield_next_case()
                return

            redirect_url = urljoin(response.url, location.decode("utf-8"))
            self.logger.info(f"[{case_number}] POST redirected to: {redirect_url}")

            meta = response.meta.copy()
            meta["redirect_hops"] = redirect_hops + 1

            yield scrapy.Request(
                url=redirect_url,
                callback=self.parse_search_results,
                meta={
                    **meta,
                    "handle_httpstatus_list": [301, 302],
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
                self._mark_case_failed(
                    case_number=case_number,
                    court_identifier=court_identifier,
                    error_message=f"Server error: {error_text}",
                )
                yield from self._yield_next_case()
                return

            # Check for no results
            if "कुनै रेकर्ड भेटिएन" in response.text:
                self.logger.warning(f"[{case_number}] No results found")
                self.failed_cases += 1
                self._mark_case_failed(
                    case_number=case_number,
                    court_identifier=court_identifier,
                    error_message="No results found (कुनै रेकर्ड भेटिएन)",
                )
                yield from self._yield_next_case()
                return

            # Find results table
            results_table = soup.find("table", class_="table table-bordered sc-table")
            if not results_table:
                results_table = soup.find("table", class_="table")

            if not results_table:
                self.logger.error(f"[{case_number}] Could not find results table")
                self.failed_cases += 1
                self._mark_case_failed(
                    case_number=case_number,
                    court_identifier=court_identifier,
                    error_message="Could not find results table",
                )
                yield from self._yield_next_case()
                return

            tbody = results_table.find("tbody")
            if not tbody:
                self.logger.error(f"[{case_number}] No tbody found in results table")
                self.failed_cases += 1
                self._mark_case_failed(
                    case_number=case_number,
                    court_identifier=court_identifier,
                    error_message="No tbody found in results table",
                )
                yield from self._yield_next_case()
                return

            rows = tbody.find_all("tr")
            self.logger.info(f"[{case_number}] Found {len(rows)} row(s)")

            # Collect all documents for this case
            documents = []

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

                    # Store document info with all enrichment data
                    documents.append(
                        {
                            "url": doc_url,
                            "court": cells[0].get_text(strip=True),
                            "registration_number": cells[1].get_text(strip=True),
                            "case_number_from_site": cells[2].get_text(strip=True),
                            "plaintiff": cells[3].get_text(strip=True),
                            "defendant": cells[4].get_text(strip=True),
                            "subject": cells[5].get_text(strip=True),
                            "location": cells[6].get_text(strip=True),
                            "status": cells[7].get_text(strip=True),
                            "decision_date": cells[8].get_text(strip=True),
                            "link_text": cells[9].get_text(strip=True),
                        }
                    )
                else:
                    self.logger.warning(f"[{case_number}] Row {idx}: No download link")

            # Save all documents at once
            if documents:
                saved = self._save_order_info(
                    case_number=case_number,
                    court_identifier=court_identifier,
                    documents=documents,
                )
                if saved:
                    self.successful_cases += 1
                else:
                    self.failed_cases += 1
                    self._mark_case_failed(
                        case_number=case_number,
                        court_identifier=court_identifier,
                        error_message="Failed to persist order info",
                    )
            else:
                # No documents found in any row
                self.failed_cases += 1
                self._mark_case_failed(
                    case_number=case_number,
                    court_identifier=court_identifier,
                    error_message="No download link found in results",
                )

        except Exception as e:
            self.logger.exception(f"[{case_number}] Exception in parse_search_results")
            self.failed_cases += 1
            self._mark_case_failed(
                case_number=case_number,
                court_identifier=court_identifier,
                error_message=str(e),
            )

        yield from self._yield_next_case()

    def _save_order_info(self, case_number, court_identifier, documents) -> bool:
        """Save order documents and enrichment data. Returns True on success."""
        try:
            with self.session.begin():
                case = (
                    self.session.query(CourtCase)
                    .filter_by(
                        case_number=case_number, court_identifier=court_identifier
                    )
                    .first()
                )

                if not case:
                    self.logger.warning(f"Case not found: {case_number}")
                    return False

                if case.extra_data is None:
                    case.extra_data = {}

                # Store all documents
                case.extra_data["order_documents"] = documents
                case.extra_data["order_document_urls"] = [
                    doc["url"] for doc in documents
                ]
                case.extra_data["order_document_url"] = documents[0][
                    "url"
                ]  # backward compat
                case.extra_data["order_found_at"] = (
                    datetime.now(KATHMANDU_TZ).replace(tzinfo=None).isoformat()
                )

                # Store first doc enrichment for backward compat
                first = documents[0]
                case.extra_data.update(
                    {
                        "enrichment_court": first.get("court"),
                        "enrichment_registration_number": first.get(
                            "registration_number"
                        ),
                        "enrichment_case_number": first.get("case_number_from_site"),
                        "enrichment_plaintiff": first.get("plaintiff"),
                        "enrichment_defendant": first.get("defendant"),
                        "enrichment_subject": first.get("subject"),
                        "enrichment_location": first.get("location"),
                        "enrichment_status": first.get("status"),
                        "enrichment_decision_date": first.get("decision_date"),
                        "enrichment_link_text": first.get("link_text"),
                    }
                )

                # Clear failure flags
                case.extra_data.pop("order_listing_failed", None)
                case.extra_data.pop("order_listing_error", None)
                case.extra_data.pop("order_listing_failed_at", None)

                flag_modified(case, "extra_data")
                self.logger.info(f"Saved {len(documents)} doc(s) for {case_number}")
                return True

        except Exception:
            self.logger.exception("Error saving order info")
            return False

    def _mark_case_failed(self, case_number, court_identifier, error_message):
        """Mark case as failed."""
        # Guard against missing identifiers
        if not case_number or not court_identifier:
            self.logger.warning(
                f"Skipping failure mark due to missing identifiers: "
                f"case_number={case_number!r}, court_identifier={court_identifier!r}"
            )
            return

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

                    case.extra_data.update(
                        {
                            "order_listing_failed": True,
                            "order_listing_error": error_message,
                            "order_listing_failed_at": datetime.now(KATHMANDU_TZ)
                            .replace(tzinfo=None)
                            .isoformat(),
                        }
                    )
                    flag_modified(case, "extra_data")

        except Exception:
            self.logger.exception("Error marking case as failed")

    def closed(self, reason):
        """Cleanup and log summary."""
        try:
            if hasattr(self, "session") and self.session:
                self.session.close()
            if hasattr(self, "engine") and self.engine:
                self.engine.dispose()

            self.logger.info(
                f"Spider closed: {reason} | Total: {self.total_cases} | "
                f"Success: {self.successful_cases} | Failed: {self.failed_cases}"
            )
        except Exception:
            self.logger.exception("Error in cleanup")
