"""
High Court Cases Scraper

Scrapes daily case lists (cause lists) from all 18 high courts in Nepal.
URL pattern: https://supremecourt.gov.np/court/{court_id}/bench_list?pesi_date={date}
"""

import scrapy
import re
from datetime import datetime, timedelta
from typing import List, Tuple
from scrapy.http import FormRequest
from bs4 import BeautifulSoup
from nepali.datetime import nepalidate
import pytz
from ngm.utils.normalizer import (
    normalize_whitespace,
    normalize_date,
    nepali_to_roman_numerals,
)
from ngm.utils.court_ids import HIGH_COURTS
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


class HighCourtCasesSpider(scrapy.Spider):
    name = "high_court_cases"

    custom_settings = {
        "CONCURRENT_REQUESTS": 2,
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 408, 429],
        "RETRY_PRIORITY_ADJUST": -1,
    }

    def __init__(self, court=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.engine = get_engine()
        init_db(self.engine)
        self.session = get_session(self.engine)
        self.case_cache = CaseCache()

        court_identifiers = [c["identifier"] for c in HIGH_COURTS]
        if court and court in court_identifiers:
            self.courts = [court]
        else:
            self.courts = court_identifiers

        self._bench_counter = {}
        self._data_by_date = {}

    def start_requests(self):
        now_ktm = datetime.now(KATHMANDU_TZ)
        end_date = now_ktm.date() - timedelta(days=SCRAPE_OFFSET_DAYS)
        start_date = end_date - timedelta(days=SCRAPE_LOOKBACK_DAYS)

        for court_id in self.courts:
            scraped_dates = get_scraped_dates(self.session, court_id)

            self.logger.info(
                f"Starting scrape for {court_id}, {len(scraped_dates)} dates already processed"
            )

            current_date = end_date
            while current_date >= start_date:
                try:
                    nepali_date = nepalidate.from_date(current_date)
                    date_bs = f"{nepali_date.year:04d}-{nepali_date.month:02d}-{nepali_date.day:02d}"

                    if date_bs in scraped_dates:
                        self.logger.debug(
                            f"Skipping {court_id} {date_bs} (already processed)"
                        )
                        current_date -= timedelta(days=1)
                        continue

                    pesi_date = f"{nepali_date.year:04d}%2F{nepali_date.month:02d}%2F{nepali_date.day:02d}"

                    self.logger.info(f"Processing {court_id} - date: {date_bs}")

                    yield scrapy.Request(
                        url=f"https://supremecourt.gov.np/court/{court_id}/bench_list?pesi_date={pesi_date}",
                        callback=self.parse_bench_list,
                        meta={
                            "court_id": court_id,
                            "date_bs": date_bs,
                            "hearing_date": f"{nepali_date.year:04d}{nepali_date.month:02d}{nepali_date.day:02d}",
                        },
                        dont_filter=True,
                    )
                except Exception as e:
                    self.logger.error(
                        f"Error converting date {current_date} for {court_id}: {e}"
                    )

                current_date -= timedelta(days=1)

    def parse_bench_list(self, response):
        soup = BeautifulSoup(response.text, "html.parser")

        court_id = response.meta["court_id"]
        date_bs = response.meta["date_bs"]
        hearing_date = response.meta["hearing_date"]

        if (
            "The requested URL was rejected" in response.text
            or "support ID is:" in response.text
        ):
            self.logger.error(f"Request blocked by WAF for {court_id} - {date_bs}")
            return

        bench_table = soup.find(
            "table", class_="table table-striped table-bordered table-hover"
        )

        if not bench_table:
            self.logger.info(f"No bench list found for {court_id} - {date_bs}")
            self._save_cases_and_hearings([], court_id, date_bs, 0)
            return

        rows = (
            bench_table.find("tbody").find_all("tr")
            if bench_table.find("tbody")
            else []
        )

        benches = []
        for row in rows:
            if "जम्माः" in row.get_text():
                continue

            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            onclick = row.get("onclick", "")
            if "send_data" in onclick:
                match = re.search(
                    r"send_data\('(\d+)',\s*'([^']+)',\s*'(\d+)'\)", onclick
                )
                if match:
                    bench_id = match.group(1)
                    bench_no = match.group(2)
                    judge_name = (
                        normalize_whitespace(cells[1].get_text())
                        if len(cells) > 1
                        else ""
                    )

                    benches.append(
                        {
                            "bench_id": bench_id,
                            "bench_no": bench_no,
                            "judge_name": judge_name,
                        }
                    )

        if not benches:
            self.logger.info(f"No benches found for {court_id} - {date_bs}")
            self._save_cases_and_hearings([], court_id, date_bs, 0)
            return

        self.logger.info(f"Found {len(benches)} benches for {court_id} - {date_bs}")

        for bench in benches:
            yield FormRequest(
                url=f"https://supremecourt.gov.np/court/{court_id}/cause_list_detail",
                formdata={
                    "bench_id": bench["bench_id"],
                    "bench_no": bench["bench_no"],
                    "hearing_date": hearing_date,
                },
                callback=self.parse_cases,
                meta={
                    "court_id": court_id,
                    "date_bs": date_bs,
                    "bench_id": bench["bench_id"],
                    "bench_no": bench["bench_no"],
                    "judge_name": bench["judge_name"],
                    "total_benches": len(benches),
                },
                dont_filter=True,
            )

    def _clean_case_number(self, case_number_cell):
        for br in case_number_cell.find_all("br"):
            br.replace_with(" ")
        case_number = normalize_whitespace(case_number_cell.get_text())
        cleaned = re.sub(r"\s*\([^)]*\)\s*", "", case_number)
        return cleaned.strip()

    def _extract_case_data(
        self, rows, court_id, date_bs, bench_id, bench_no, bench_type, judge_name
    ) -> List[Tuple[CourtCase, CourtCaseHearing]]:
        data: List[Tuple[CourtCase, CourtCaseHearing]] = []

        bench_no_roman = nepali_to_roman_numerals(bench_no)

        for row in rows:
            cells = row.find_all("td")

            if len(cells) < 9:
                continue

            serial_no = nepali_to_roman_numerals(
                normalize_whitespace(cells[0].get_text())
            )
            division = normalize_whitespace(cells[1].get_text())
            registration_date = normalize_date(
                normalize_whitespace(cells[2].get_text())
            )
            case_type = normalize_whitespace(cells[3].get_text())
            case_number = self._clean_case_number(cells[4])

            parties = normalize_whitespace(cells[5].get_text())
            plaintiff = ""
            defendant = ""
            if "||" in parties:
                parts = parties.split("||", 1)
                plaintiff = normalize_whitespace(parts[0])
                defendant = normalize_whitespace(parts[1])
            else:
                plaintiff = parties

            lawyers_text = normalize_whitespace(cells[6].get_text())
            lawyer_names = (
                None if not lawyers_text or lawyers_text == "--" else lawyers_text
            )

            remarks = normalize_whitespace(cells[7].get_text())

            status_cell = cells[8]
            for br in status_cell.find_all("br"):
                br.replace_with("\n")
            status = normalize_whitespace(status_cell.get_text())

            if not case_number:
                continue

            case = self.case_cache.get(case_number, court_id)
            if not case:
                case = CourtCase(
                    case_number=case_number,
                    court_identifier=court_id,
                    registration_date_bs=registration_date,
                    registration_date_ad=convert_bs_to_ad(registration_date),
                    case_type=case_type,
                    division=division,
                    plaintiff=plaintiff,
                    defendant=defendant,
                )
                self.case_cache.set(case)

            hearing = CourtCaseHearing(
                case_number=case_number,
                court_identifier=court_id,
                hearing_date_bs=date_bs,
                hearing_date_ad=convert_bs_to_ad(date_bs),
                bench=bench_no_roman,
                bench_type=bench_type,
                judge_names=judge_name,
                lawyer_names=lawyer_names,
                serial_no=serial_no,
                case_status=status,
                remarks=remarks,
                scraped_at=datetime.now(KATHMANDU_TZ).replace(tzinfo=None),
                extra_data={"bench_id": bench_id, "bench_no": bench_no},
            )

            data.append((case, hearing))

        return data

    def _save_cases_and_hearings(
        self,
        data: List[Tuple[CourtCase, CourtCaseHearing]],
        court_id: str,
        date_bs: str,
        bench_count: int,
    ):
        with self.session.begin():
            for case, hearing in data:
                self.session.merge(case)
                self.session.add(hearing)

            mark_date_scraped(self.session, court_id, date_bs, f"{bench_count} benches")

    def _handle_bench_completion(
        self,
        court_id: str,
        date_bs: str,
        total_benches: int,
        new_data: List[Tuple[CourtCase, CourtCaseHearing]],
    ):
        key = (court_id, date_bs)
        self._bench_counter[key] = self._bench_counter.get(key, 0) + 1

        if self._bench_counter[key] >= total_benches:
            all_data = self._data_by_date.get(key, [])
            all_data.extend(new_data)
            self._save_cases_and_hearings(all_data, court_id, date_bs, total_benches)
            self.logger.info(f"Saved all cases for {court_id} on {date_bs}")
            self._data_by_date.pop(key, None)
            self._bench_counter.pop(key, None)  # Clean up counter
        else:
            if key not in self._data_by_date:
                self._data_by_date[key] = []
            self._data_by_date[key].extend(new_data)

    def parse_cases(self, response):
        soup = BeautifulSoup(response.text, "html.parser")

        court_id = response.meta["court_id"]
        date_bs = response.meta["date_bs"]
        bench_id = response.meta["bench_id"]
        bench_no = response.meta["bench_no"]
        judge_name = response.meta["judge_name"]
        total_benches = response.meta["total_benches"]

        bench_type_elem = soup.find("h4", string=lambda x: x and "इजलास" in x)
        bench_type = (
            normalize_whitespace(bench_type_elem.get_text()) if bench_type_elem else ""
        )

        case_table = soup.find("table", class_="table table-bordered table-hover")

        if not case_table:
            self.logger.warning(
                f"No case table found for {court_id} - bench {bench_no} on {date_bs}"
            )
            self._handle_bench_completion(court_id, date_bs, total_benches, [])
            return

        rows = (
            case_table.find("tbody").find_all("tr", class_="data_row")
            if case_table.find("tbody")
            else []
        )

        if not rows:
            self.logger.info(
                f"No cases found for {court_id} - bench {bench_no} on {date_bs}"
            )
            self._handle_bench_completion(court_id, date_bs, total_benches, [])
            return

        data = self._extract_case_data(
            rows, court_id, date_bs, bench_id, bench_no, bench_type, judge_name
        )

        self.logger.info(
            f"Extracted {len(data)} cases for {court_id} - bench {bench_no} on {date_bs}"
        )
        self._handle_bench_completion(court_id, date_bs, total_benches, data)
