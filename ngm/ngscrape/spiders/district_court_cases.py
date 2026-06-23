"""
District Court Cases Scraper

Scrapes daily case lists (pesi) from all district courts in Nepal.
URL pattern: https://supremecourt.gov.np/weekly_dainik/pesi/daily/{district_id}
POST params: todays_date (BS), pesi_date (yyyy-mm-dd BS)
"""

import scrapy
from datetime import datetime, timedelta
from typing import List, Tuple
from scrapy.crawler import CrawlerProcess
from scrapy.http import FormRequest
from bs4 import BeautifulSoup
from nepali.datetime import nepalidate
import pytz
from ngm.utils.normalizer import (
    normalize_whitespace,
    normalize_date,
    nepali_to_roman_numerals,
)
from ngm.utils.court_ids import DISTRICT_COURTS
from ngm.database.models import (
    get_engine,
    get_session,
    init_db,
    CourtCase,
    CourtCaseHearing,
)
from ngm.utils.db_helpers import (
    get_scraped_dates,
    mark_date_scraped,
    convert_bs_to_ad,
    CaseCache,
)
from ngm.ngscrape.constants import SCRAPE_LOOKBACK_DAYS, SCRAPE_OFFSET_DAYS

KATHMANDU_TZ = pytz.timezone("Asia/Kathmandu")


class DistrictCourtCasesSpider(scrapy.Spider):
    name = "district_court_cases"
    base_url = "https://supremecourt.gov.np/weekly_dainik/pesi/daily/{district_id}"

    custom_settings = {
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 408, 429],
        "RETRY_PRIORITY_ADJUST": -1,
    }

    async def start(self):
        """Generate requests for all district courts"""
        self.engine = get_engine()
        init_db(self.engine)
        self.session = get_session(self.engine)
        self.case_cache = CaseCache()

        now_ktm = datetime.now(KATHMANDU_TZ)
        end_date = now_ktm.date() - timedelta(days=SCRAPE_OFFSET_DAYS)
        start_date = end_date - timedelta(days=SCRAPE_LOOKBACK_DAYS)

        for court in DISTRICT_COURTS:
            code_name = court["code_name"]
            district_id = court["district_id"]
            district_name = court["district"]

            scraped_dates = get_scraped_dates(self.session, code_name)

            self.logger.info(
                f"Starting scrape for {district_name} ({code_name}), "
                f"id={district_id}, {len(scraped_dates)} dates already processed"
            )

            current_date = end_date
            while current_date >= start_date:
                try:
                    nepali_date = nepalidate.from_date(current_date)
                    pesi_date = f"{nepali_date.year}-{str(nepali_date.month).zfill(2)}-{str(nepali_date.day).zfill(2)}"

                    if pesi_date in scraped_dates:
                        self.logger.debug(
                            f"Skipping {code_name} {pesi_date} (already processed)"
                        )
                        current_date -= timedelta(days=1)
                        continue

                    todays_nepali = nepalidate.from_date(datetime.now().date())
                    todays_date = f"{todays_nepali.year}-{str(todays_nepali.month).zfill(2)}-{str(todays_nepali.day).zfill(2)}"

                    url = self.base_url.format(district_id=district_id)

                    yield FormRequest(
                        url=url,
                        method="POST",
                        formdata={
                            "todays_date": todays_date,
                            "pesi_date": pesi_date,
                            "submit": "खोज्नु होस्",
                        },
                        callback=self.parse_daily_list,
                        meta={
                            "code_name": code_name,
                            "district_id": district_id,
                            "district_name": district_name,
                            "date_bs": pesi_date,
                        },
                        dont_filter=True,
                    )
                except Exception as e:
                    self.logger.error(f"Error converting date {current_date}: {e}")

                current_date -= timedelta(days=1)

    def _extract_case_data(
        self, case_tables, code_name: str, date_bs: str
    ) -> List[Tuple[CourtCase, CourtCaseHearing]]:
        """Extract and construct SQLAlchemy objects from table rows."""
        data: List[Tuple[CourtCase, CourtCaseHearing]] = []
        current_bench = None
        current_judge = None

        for table in case_tables:
            prev_table = table.find_previous_sibling("table")
            if prev_table:
                bench_row = prev_table.find("tr")
                if bench_row:
                    bench_td = bench_row.find("td", align="right")
                    judge_td = bench_row.find("td", class_="judge")
                    if bench_td:
                        current_bench = normalize_whitespace(bench_td.get_text())
                    if judge_td:
                        current_judge = normalize_whitespace(judge_td.get_text())

            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all("td")

                if len(cells) < 10 or row.find("th"):
                    continue

                try:
                    serial_no = nepali_to_roman_numerals(
                        normalize_whitespace(cells[0].get_text())
                    )

                    case_parts = cells[1].get_text(separator="\n").strip().split("\n")
                    case_number = (
                        nepali_to_roman_numerals(normalize_whitespace(case_parts[0]))
                        if case_parts
                        else ""
                    )
                    case_id = (
                        nepali_to_roman_numerals(
                            normalize_whitespace(case_parts[1].strip("()"))
                        )
                        if len(case_parts) > 1
                        else ""
                    )

                    # Handle secondary case number (when mudda no is split across two lines)
                    secondary_case_number = None
                    if len(case_parts) >= 2:
                        secondary_case_number = nepali_to_roman_numerals(
                            normalize_whitespace(case_parts[-1].strip("()"))
                        )

                    reg_date_parts = (
                        cells[2].get_text(separator="\n").strip().split("\n")
                    )
                    registration_date = (
                        normalize_date(normalize_whitespace(reg_date_parts[0]))
                        if reg_date_parts
                        else ""
                    )
                    case_type = normalize_whitespace(cells[3].get_text())[:200]
                    plaintiff = normalize_whitespace(cells[4].get_text())
                    defendant = normalize_whitespace(cells[5].get_text())
                    section = normalize_whitespace(cells[6].get_text())[:200] or ""
                    priority = normalize_whitespace(cells[7].get_text())[:400] or ""
                    remarks = normalize_whitespace(cells[8].get_text()) or ""
                    decision_type = (
                        normalize_whitespace(cells[9].get_text())[:200] or ""
                    )

                    if not case_number:
                        continue

                    case = self.case_cache.get(case_number, code_name)
                    if not case:
                        extra_data = {}
                        if secondary_case_number:
                            extra_data["secondary_case_number"] = secondary_case_number

                        case = CourtCase(
                            case_number=case_number,
                            court_identifier=code_name,
                            registration_date_bs=registration_date,
                            registration_date_ad=convert_bs_to_ad(registration_date),
                            case_type=case_type,
                            plaintiff=plaintiff,
                            defendant=defendant,
                            section=section,
                            priority=priority,
                            case_id=case_id,
                            extra_data=extra_data if extra_data else None,
                        )
                        self.case_cache.set(case)

                    hearing = CourtCaseHearing(
                        case_number=case_number,
                        court_identifier=code_name,
                        hearing_date_bs=date_bs,
                        hearing_date_ad=convert_bs_to_ad(date_bs),
                        bench=current_bench,
                        judge_names=current_judge,
                        serial_no=serial_no,
                        decision_type=decision_type,
                        remarks=remarks,
                        scraped_at=datetime.now(KATHMANDU_TZ).replace(tzinfo=None),
                    )

                    data.append((case, hearing))

                except Exception as e:
                    self.logger.error(f"Error parsing row: {e}")
                    continue

        return data

    def _save_cases_and_hearings(
        self,
        data: List[Tuple[CourtCase, CourtCaseHearing]],
        code_name: str,
        date_bs: str,
    ):
        """Save cases and hearings in a transaction."""
        with self.session.begin():
            for case, hearing in data:
                self.session.merge(case)
                self.session.add(hearing)

            mark_date_scraped(self.session, code_name, date_bs)

    def parse_daily_list(self, response):
        """Parse the daily case list response"""
        soup = BeautifulSoup(response.text, "html.parser")

        code_name = response.meta["code_name"]
        date_bs = response.meta["date_bs"]

        error_div = soup.find("div", class_="alert_error")
        if error_div and "Causelist is not available" in error_div.get_text():
            self.logger.info(f"No cases for {code_name} on {date_bs}")
            self._save_cases_and_hearings([], code_name, date_bs)
            return

        case_tables = soup.find_all("table", {"border": "1", "class": "record_display"})

        if not case_tables:
            self.logger.info(f"No case tables found for {code_name} on {date_bs}")
            self._save_cases_and_hearings([], code_name, date_bs)
            return

        data = self._extract_case_data(case_tables, code_name, date_bs)
        self._save_cases_and_hearings(data, code_name, date_bs)

        self.logger.info(f"Saved {len(data)} cases for {code_name} on {date_bs}")


if __name__ == "__main__":
    process = CrawlerProcess({"LOG_LEVEL": "INFO"})
    process.crawl(DistrictCourtCasesSpider)
    process.start()
