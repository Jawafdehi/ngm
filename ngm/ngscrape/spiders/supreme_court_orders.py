"""
Supreme Court Orders Spider (Listing Only)

Scrapes basic order information from Supreme Court website.
This spider only lists cases with orders - it does NOT download documents.
Use supreme_orders_enrichment.py to download the actual order documents.

Target: https://supremecourt.gov.np/cp/
"""
import urllib.parse
import scrapy
import re
from urllib.parse import urljoin
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
        "DOWNLOAD_DELAY": 3,
        "DOWNLOAD_TIMEOUT": 60,
        "COOKIES_ENABLED": True,
        "REDIRECT_ENABLED": False,  # Handle redirects manually in parse()
        "USER_AGENT": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "DEFAULT_REQUEST_HEADERS": {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
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
        self.engine = get_engine()
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
                    CourtCase.verdict_date_bs != "**** ** **"
                ),
                CourtCase.case_status.like("%फैसला%")
            )
            query = query.filter(has_decision)

            # Filter: Not ongoing
            query = query.filter(
                or_(
                    CourtCase.case_status.is_(None),
                    ~CourtCase.case_status.like("%चालु%")
                )
            )

            # Filter: Not already scraped
            query = query.filter(
                or_(
                    CourtCase.extra_data.is_(None),
                    CourtCase.extra_data['orders_scraped'].astext.is_(None),
                    CourtCase.extra_data['orders_scraped'].astext != 'true'
                )
            )

            # Priority ordering: Special → Supreme → High → District
            court_priority = sql_case(
                (Court.court_type == 'special', 1),
                (Court.court_type == 'supreme', 2),
                (Court.court_type == 'high', 3),
                (Court.court_type == 'district', 4),
                else_=5
            )

            query = query.order_by(
                court_priority,
                CourtCase.registration_date_ad.asc().nullslast()
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
                    self._pending_cases.append({
                        'case_number': case.case_number,
                        'court_identifier': case.court_identifier,
                        'court_type': court_type,
                        'court_id': court_id,
                        'registration_date_bs': case.registration_date_bs or '',
                    })
                except Exception as e:
                    self.logger.error(f"Error preparing case {case.case_number}: {e}")
                    self.failed_cases += 1

        self.logger.info(f"Starting to process {self.total_cases} cases")
        yield from self._yield_next_case()

    def _yield_next_case(self):
        """Yield request for the next pending case."""
        if not self._pending_cases:
            return

        case_data = self._pending_cases.pop(0)  # plain dict, no ORM
        yield scrapy.Request(
            url="https://supremecourt.gov.np/cp/",
            callback=self.parse,
            meta={
                **case_data,
                'handle_httpstatus_list': [302],
            },
            dont_filter=True
        )

    def parse(self, response):
        """Extract CAPTCHA and submit search form, handling redirects manually."""
        max_retries = 3
        retry_count = response.meta.get('retry_count', 0)

        try:
            # Handle redirect manually so we can capture the CAPTCHA cookie
            # before the server refreshes the session on redirect
            if response.status in (301, 302):
                redirect_url = response.headers.get('Location', b'').decode('utf-8')

                captcha_from_redirect = self._extract_captcha_from_session_cookie(response)

                meta = response.meta.copy()
                if captcha_from_redirect:
                    meta['captcha_solution'] = captcha_from_redirect
                    self.logger.info(f"Captured CAPTCHA from redirect: {captcha_from_redirect}")

                yield scrapy.Request(
                    url=redirect_url,
                    callback=self.parse,
                    meta={
                        **meta,
                        'handle_httpstatus_list': [302],
                    },
                    dont_filter=True
                )
                return

            # Check if CAPTCHA was already captured during redirect
            captcha_solution = response.meta.get('captcha_solution')

            # If not, try to extract from current response cookies
            if not captcha_solution:
                captcha_solution = self._extract_captcha_from_session_cookie(response)

            if captcha_solution:
                self.logger.info(f"Extracted CAPTCHA: {captcha_solution}")
                yield self.submit_search_form(response, captcha_solution)
            else:
                if retry_count < max_retries:
                    self.logger.warning(f"Failed to extract CAPTCHA, retry {retry_count + 1}/{max_retries}")
                    meta = response.meta.copy()
                    meta['retry_count'] = retry_count + 1
                    meta.pop('captcha_solution', None)
                    yield scrapy.Request(
                        url="https://supremecourt.gov.np/cp/",
                        callback=self.parse,
                        meta=meta,
                        dont_filter=True
                    )
                else:
                    case_number = response.meta.get('case_number')
                    court_identifier = response.meta.get('court_identifier')
                    self.logger.error(f"Failed to extract CAPTCHA after {max_retries} attempts for case {case_number}")
                    self.failed_cases += 1
                    self._mark_case_failed(
                        case_number=case_number,
                        court_identifier=court_identifier,
                        error_message=f"Failed to extract CAPTCHA after {max_retries} attempts"
                    )
                    yield from self._yield_next_case()  


        except Exception as e:
            self.logger.error(f"Error in parse: {e}")
            self.failed_cases += 1
            case_number = response.meta.get('case_number')
            court_identifier = response.meta.get('court_identifier')
            self._mark_case_failed(
                case_number=case_number,
                court_identifier=court_identifier,
                error_message=str(e)
            )

    def _extract_captcha_from_session_cookie(self, response):
        """Extract CAPTCHA answer from leaked session cookie."""
        for header in response.headers.getlist(b'Set-Cookie'):
            val = header.decode('utf-8', errors='ignore')

            if 'court_session=' not in val:
                continue

            unquoted = urllib.parse.unquote(val)
            match = re.search(r'"captcha_word";s:\d+:"([^"]+)"', unquoted)
            if match:
                return match.group(1)

        return None

    def submit_search_form(self, response, captcha_solution):
        """Submit the search form with case details and CAPTCHA."""
        court_type = response.meta['court_type']
        court_id = response.meta['court_id']
        case_no = response.meta['case_number']
        registration_date = response.meta['registration_date_bs']

        formdata = {
            'court_type': court_type,
            'court_id': court_id,
            'regno': case_no,
            'darta_date': registration_date,
            'faisala_date': '',
            'captcha': captcha_solution,
            'submit': 'submit'
        }

        self.logger.info(f"[{case_no}] ─── Submitting form ───")
        self.logger.info(f"[{case_no}] Submitting to URL: {response.url}")
        self.logger.info(f"[{case_no}] Form data: {formdata}")

        return FormRequest.from_response(
            response,
            formdata=formdata,
            callback=self.parse_search_results,
            headers={
                'Referer': response.url,
                'Origin': 'https://supremecourt.gov.np',
            },
             meta={
            **response.meta,
            'handle_httpstatus_list': [302],  
            },
            dont_filter=True
        )

    def parse_search_results(self, response):
        """Parse search results and save order information to database."""
        case_number = response.meta.get('case_number')
        court_identifier = response.meta.get('court_identifier')

        # ── DIAGNOSTIC LOGGING ──────────────────────────────────────────
        self.logger.info(f"[{case_number}] ─── parse_search_results called ───")
        self.logger.info(f"[{case_number}] Response URL:    {response.url}")
        self.logger.info(f"[{case_number}] Response status: {response.status}")
        self.logger.info(f"[{case_number}] Response size:   {len(response.text)} chars")
        self.logger.info(f"[{case_number}] court_type:        {response.meta.get('court_type')}")
        self.logger.info(f"[{case_number}] court_id:          {response.meta.get('court_id')}")
        self.logger.info(f"[{case_number}] registration_date: {response.meta.get('registration_date_bs')}")
        self.logger.info(f"[{case_number}] captcha_used:      {response.meta.get('captcha_solution')}")
        self.logger.info(f"[{case_number}] Response HTML (first 1000 chars):")
        self.logger.info(response.text[:1000])
        # ────────────────────────────────────────────────────────────────
        if response.status == 302:
            redirect_url = response.headers.get('Location', b'').decode('utf-8')
            self.logger.info(f"[{case_number}] POST redirected to: {redirect_url}")
            yield scrapy.Request(
                url=redirect_url,
                callback=self.parse_search_results,
                meta={
                    **response.meta,
                    'handle_httpstatus_list': [302],
                },
                dont_filter=True
            )
            return

        try:
            soup = BeautifulSoup(response.text, 'html.parser')

            # Check for errors
            error_table = soup.find('table', bgcolor='#FF6600')
            if error_table:
                error_text = error_table.get_text(strip=True)
                self.logger.error(f"[{case_number}] Server error response: {error_text}")
                self.failed_cases += 1
                return

            # Check for no results
            if 'कुनै रेकर्ड भेटिएन' in response.text:
                self.logger.warning(f"[{case_number}] No results found")
                self.failed_cases += 1
                return

            # Log all tables found on the page for diagnosis
            all_tables = soup.find_all('table')
            self.logger.info(f"[{case_number}] Tables found on page: {len(all_tables)}")
            for i, t in enumerate(all_tables):
                self.logger.info(f"[{case_number}]   Table {i}: class={t.get('class')} bgcolor={t.get('bgcolor')}")

            # Find results table
            results_table = soup.find('table', class_='table table-bordered sc-table')
            if not results_table:
                self.logger.info(f"[{case_number}] sc-table not found, trying generic .table")
                results_table = soup.find('table', class_='table')

            if not results_table:
                self.logger.error(f"[{case_number}] Could not find results table")
                self.logger.info(f"[{case_number}] Full HTML dump:")
                self.logger.info(response.text)
                self.failed_cases += 1
                return

            tbody = results_table.find('tbody')
            if not tbody:
                self.logger.error(f"[{case_number}] No tbody found in results table")
                self.failed_cases += 1
                return

            rows = tbody.find_all('tr')
            self.logger.info(f"[{case_number}] ✓ Found {len(rows)} row(s) in results table")

            for idx, row in enumerate(rows, 1):
                cells = row.find_all('td')
                self.logger.info(f"[{case_number}] Row {idx}: {len(cells)} cells")

                if len(cells) < 10:
                    self.logger.warning(f"[{case_number}] Row {idx}: Unexpected format ({len(cells)} cells), skipping")
                    continue

                self.logger.info(f"[{case_number}] Row {idx} cell[1] (reg no):   {cells[1].get_text(strip=True)}")
                self.logger.info(f"[{case_number}] Row {idx} cell[2] (case no):  {cells[2].get_text(strip=True)}")
                self.logger.info(f"[{case_number}] Row {idx} cell[8] (decision): {cells[8].get_text(strip=True)}")

                doc_link = cells[9].find('a', class_='download_content')
                if doc_link:
                    self.logger.info(f"[{case_number}] Row {idx} doc link: {doc_link.get('href')}")
                else:
                    self.logger.warning(f"[{case_number}] Row {idx} cell[9] raw: {cells[9]}")

                doc_url = None
                if doc_link and doc_link.get('href'):
                    doc_url = urljoin(response.url, doc_link['href'])
                    self.logger.info(f"[{case_number}] ✓ Document URL: {doc_url}")

                    self._save_order_info(
                        case_number=response.meta['case_number'],
                        court_identifier=response.meta['court_identifier'],
                        document_url=doc_url
                    )
                    self.successful_cases += 1
                else:
                    self.logger.warning(f"[{case_number}] ⚠ Row {idx}: No download link found")
                    self.failed_cases += 1

        except Exception as e:
            self.logger.error(f"[{case_number}] Exception in parse_search_results: {e}", exc_info=True)
            self.failed_cases += 1
            
        yield from self._yield_next_case()


    def _save_order_info(self, case_number, court_identifier, document_url):
        """Save order document URL to database."""
        try:
            with self.session.begin():
                case = self.session.query(CourtCase).filter_by(
                    case_number=case_number,
                    court_identifier=court_identifier
                ).first()

                if case:
                    if case.extra_data is None:
                        case.extra_data = {}

                    case.extra_data['order_document_url'] = document_url
                    case.extra_data['order_found_at'] = datetime.now().isoformat()
                    case.status = 'pending'
                    flag_modified(case, 'extra_data')

                    self.logger.info(f"Saved order info for case {case_number}")
                else:
                    self.logger.warning(f"Case not found in database: {case_number}")

        except Exception as e:
            self.logger.error(f"Error saving order info: {e}")

    def _mark_case_failed(self, case_number, court_identifier, error_message):
        """Mark a case as failed in the database."""
        try:
            with self.session.begin():
                case = self.session.query(CourtCase).filter_by(
                    case_number=case_number,
                    court_identifier=court_identifier
                ).first()

                if case:
                    if case.extra_data is None:
                        case.extra_data = {}

                    case.extra_data['order_listing_failed'] = True
                    case.extra_data['order_listing_error'] = error_message
                    case.extra_data['order_listing_failed_at'] = datetime.now().isoformat()
                    flag_modified(case, 'extra_data')

                    self.logger.info(f"Marked case {case_number} as failed in database")
                else:
                    self.logger.warning(f"Case not found in database: {case_number}")

        except Exception as e:
            self.logger.error(f"Error marking case as failed: {e}")

    def closed(self, reason):
        """Spider cleanup and summary logging."""
        try:
            if hasattr(self, 'session') and self.session:
                self.session.close()
                self.logger.info("Database session closed")

            if hasattr(self, 'engine') and self.engine:
                self.engine.dispose()
                self.logger.info("Database engine disposed")

            self.logger.info("=" * 60)
            self.logger.info("SPIDER SUMMARY")
            self.logger.info("=" * 60)
            self.logger.info(f"Total cases processed: {self.total_cases}")
            self.logger.info(f"Successful: {self.successful_cases}")
            self.logger.info(f"Failed: {self.failed_cases}")
            self.logger.info(f"Reason: {reason}")
            self.logger.info("=" * 60)

        except Exception as e:
            self.logger.error(f"Error in cleanup: {e}")