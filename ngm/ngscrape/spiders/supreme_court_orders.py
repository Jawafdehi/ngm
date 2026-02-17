"""
Supreme Court Orders Spider
Scrapes court orders/decisions from Supreme Court website.
Extracts CAPTCHA session cookie

Target: https://supremecourt.gov.np/cp/
"""
import urllib.parse
import scrapy
import os
import re
from urllib.parse import urljoin
from datetime import datetime
from scrapy.http import FormRequest
from bs4 import BeautifulSoup

from ngm.ngscrape.settings import FILES_STORE


class SupremeCourtOrdersSpider(scrapy.Spider):
    name = "supreme_court_orders"
    allowed_domains = ["supremecourt.gov.np"]
    start_urls = ["https://supremecourt.gov.np/cp/"]
    
    custom_settings = {
        "ITEM_PIPELINES": {
            'ngm.ngscrape.pipelines.SupremeCourtOrdersPipeline': 300,
        },
        "FILES_STORE": os.path.join(FILES_STORE, "supreme-court/orders/"),
        "CONCURRENT_REQUESTS": 1,
        "DOWNLOAD_DELAY": 2,
        "USER_AGENT": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "DEFAULT_REQUEST_HEADERS": {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-User': '?1',
        },
    }
    
    def __init__(self, court_type="S", court_id="", case_no="", 
                 registration_date="", *args, **kwargs):
        """
        Initialize spider with case parameters.
        
        Args:
            court_type: S=Supreme, A=High, D=District, T=Special, AD=Administrative
            court_id: Court ID (optional, depends on court type)
            case_no: Case registration number (empty for all cases)
            registration_date: Registration date in BS format (e.g., "2080-01-05")
        """
        super().__init__(*args, **kwargs)
        self.court_type = court_type
        self.court_id = court_id
        self.case_no = case_no
        self.registration_date = registration_date
        
        self.logger.info(f"Court: {court_type}, Case: {case_no or 'ALL'}, Date: {registration_date}")
        
    def parse(self, response):
        captcha_solution = self._extract_captcha_from_session_cookie(response)

        if captcha_solution:
            self.logger.info(f"Extracted CAPTCHA: {captcha_solution}")
            yield self.submit_search_form(response, captcha_solution)
        else:
            self.logger.error("Failed to extract CAPTCHA from session cookie")

    def _extract_captcha_from_session_cookie(self, response):
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
        """Submit the search form with case details and CAPTCHA solution."""
        formdata = {
            'court_type': self.court_type,
            'court_id': self.court_id,
            'regno': self.case_no,
            'darta_date': self.registration_date,
            'faisala_date': '',
            'captcha': captcha_solution,
            'submit': 'submit'
        }
        
        return FormRequest.from_response(
            response,
            formdata=formdata,
            callback=self.parse_search_results,
            headers={
                'Referer': response.url,
                'Origin': 'https://supremecourt.gov.np',
            },
            meta={
                'case_no': self.case_no,
                'registration_date': self.registration_date,
                'court_type': self.court_type,
            },
            dont_filter=True
        )
    
    def parse_search_results(self, response):
        """Parse search results and extract case order details."""
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Check for errors
        error_table = soup.find('table', bgcolor='#FF6600')
        if error_table:
            error_text = error_table.get_text(strip=True)
            self.logger.error(f"Error: {error_text}")
            return
        
        # Check for no results
        if 'कुनै रेकर्ड भेटिएन' in response.text:
            self.logger.warning("No results found")
            return
        
        # Log success message
        upload_status = soup.find('h3', id='uploadStatus')
        if upload_status:
            self.logger.info(f"✓ {upload_status.get_text().strip()}")
        
        # Find results table
        results_table = soup.find('table', class_='table table-bordered sc-table')
        if not results_table:
            results_table = soup.find('table', class_='table')
        
        if not results_table:
            self.logger.error("Could not find results table")
            return
        
        tbody = results_table.find('tbody')
        if not tbody:
            self.logger.error("No tbody found in results table")
            return
            
        rows = tbody.find_all('tr')
        self.logger.info(f"✓ Found {len(rows)} case(s)")
        
        for idx, row in enumerate(rows, 1):
            cells = row.find_all('td')
            
            if len(cells) < 10:
                self.logger.warning(f"Row {idx}: Unexpected format ({len(cells)} cells)")
                continue
            
            serial_no = cells[0].get_text(strip=True)
            registration_no = cells[1].get_text(strip=True)
            case_no = cells[2].get_text(strip=True)
            registration_date = cells[3].get_text(strip=True)
            case_type = cells[4].get_text(strip=True)
            case_name = cells[5].get_text(strip=True)
            plaintiff = cells[6].get_text(strip=True)
            defendant = cells[7].get_text(strip=True)
            decision_date = cells[8].get_text(strip=True)
            
            doc_link = cells[9].find('a', class_='download_content')
            doc_url = None
            
            if doc_link and doc_link.get('href'):
                doc_url = urljoin(response.url, doc_link['href'])
                self.logger.info(f"✓ Case {idx}: {registration_no} - {doc_url}")
            else:
                self.logger.warning(f"⚠ Case {idx}: {registration_no} - No document")
            
            yield {
                'serial_no': serial_no,
                'registration_no': registration_no,
                'case_no': case_no,
                'registration_date': registration_date,
                'case_type': case_type,
                'case_name': case_name,
                'plaintiff': plaintiff,
                'defendant': defendant,
                'decision_date': decision_date,
                'document_url': doc_url,
                'court_type': response.meta['court_type'],
                'source_url': response.url,
                'scraped_at': datetime.now().isoformat(),
                'file_urls': [doc_url] if doc_url else [],
            }


if __name__ == "__main__":
    from scrapy.crawler import CrawlerProcess
    
    process = CrawlerProcess({
        "LOG_LEVEL": "INFO",
    })
    
    process.crawl(
        SupremeCourtOrdersSpider,
        court_type="S",  # सर्वोच्च अदालत (Supreme Court)
        court_id="",
        case_no="073-CR-0023",
        registration_date="",
    )
    
    process.start()
