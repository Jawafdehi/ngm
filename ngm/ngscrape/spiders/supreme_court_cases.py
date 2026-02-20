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
from ngm.ngscrape.constants import (
    SCRAPE_LOOKBACK_DAYS_SUPREME_COURT,
    SCRAPE_OFFSET_DAYS,
)

COURT_ID = "supreme"
KATHMANDU_TZ = pytz.timezone("Asia/Kathmandu")


class SupremeCourtCasesSpider(scrapy.Spider):
    name = "supreme_court_cases"
    base_url = (
        "https://supremecourt.gov.np/lic/sys.php?d=reports&f=weekly_suppli_public"
    )

    custom_settings = {
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 408, 429],
        "RETRY_PRIORITY_ADJUST": -1,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.engine = get_engine()
        init_db(self.engine)
        self.session = get_session(self.engine)
        self.case_cache = CaseCache()
        self.scraped_dates = get_scraped_dates(self.session, COURT_ID)

    def _find_case_table(self, soup):
        table = soup.find(
            "table",
            {
                "width": "100%",
                "border": "0",
                "cellspacing": "0",
                "bordercolor": "#ffffff",
            },
        )
        if table and self._validate_case_table(table):
            return table

        all_tables = soup.find_all("table")
        for table in all_tables:
            header_row = table.find("tr", bgcolor="#FFCC00")
            if not header_row:
                rows = table.find_all("tr")
                if rows:
                    first_row = rows[0]
                    if first_row.get("bgcolor") == "#FFCC00":
                        header_row = first_row

            if header_row:
                header_text = header_row.get_text()
                if (
                    "क्र" in header_text
                    and "मुद्दा नं" in header_text
                    and "पक्ष" in header_text
                ):
                    if self._validate_case_table(table):
                        return table

        for table in all_tables:
            rows = table.find_all("tr")
            if not rows:
                continue

            first_row = rows[0]
            cells = first_row.find_all(["td", "th"])
            if len(cells) == 10:
                if self._validate_case_table(table):
                    return table

        return None

    def _validate_case_table(self, table):
        if not table:
            return False

        rows = table.find_all("tr")
        if len(rows) < 2:
            return False

        header_row = rows[0]
        header_cells = header_row.find_all(["td", "th"])
        if len(header_cells) != 10:
            return False

        return True

    def _find_case_rows(self, table):
        return table.find_all("tr", bgcolor="#ffffff")

    def _clean_case_number(self, case_number):
        if not case_number:
            return case_number

        import re

        cleaned = re.sub(r"\s*\([^)]*\)\s*", "", case_number)
        return cleaned.strip()

    def _clean_division(self, division):
        if not division:
            return division

        cleaned = division.strip()
        if cleaned.startswith("- "):
            cleaned = cleaned[2:]
        if cleaned.endswith(" _"):
            cleaned = cleaned[:-2]

        return cleaned.strip()

    def _parse_judges(self, cell):
        """Parse judges from a cell, handling <br> tags. Returns newline-separated string."""
        if not cell:
            return None

        for br in cell.find_all("br"):
            br.replace_with("\n")

        judges_text = cell.get_text()
        judge_names = [
            normalize_whitespace(name)
            for name in judges_text.split("\n")
            if normalize_whitespace(name)
        ]

        return "\n".join(judge_names) if judge_names else None

    def start_requests(self):
        now_ktm = datetime.now(KATHMANDU_TZ)
        end_date = now_ktm.date() - timedelta(days=SCRAPE_OFFSET_DAYS)
        start_date = end_date - timedelta(days=SCRAPE_LOOKBACK_DAYS_SUPREME_COURT)

        current_date = end_date
        while current_date >= start_date:
            try:
                nepali_date = nepalidate.from_date(current_date)
                syy = str(nepali_date.year)
                smm = str(nepali_date.month).zfill(2)
                sdd = str(nepali_date.day).zfill(2)
                date_bs = f"{syy}-{smm}-{sdd}"

                if date_bs in self.scraped_dates:
                    self.logger.debug(f"Skipping already processed date: {date_bs}")
                    current_date -= timedelta(days=1)
                    continue

                self.logger.info(f"Processing date: {current_date} -> BS {date_bs}")

                yield FormRequest(
                    url=self.base_url,
                    formdata={
                        "syy": syy,
                        "smm": smm,
                        "sdd": sdd,
                        "mode": "show",
                        "yo": "1",
                    },
                    headers={
                        "Referer": "https://supremecourt.gov.np/",
                        "Origin": "https://supremecourt.gov.np",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    callback=self.parse_cases,
                    meta={"date_bs": date_bs, "syy": syy, "smm": smm, "sdd": sdd},
                    dont_filter=True,
                )
            except Exception as e:
                self.logger.error(f"Error converting date {current_date}: {e}")

            current_date -= timedelta(days=1)

    def _extract_case_data(
        self, rows, date_bs
    ) -> List[Tuple[CourtCase, CourtCaseHearing]]:
        """Extract and construct SQLAlchemy objects from table rows."""
        data: List[Tuple[CourtCase, CourtCaseHearing]] = []

        for row in rows:
            cells = row.find_all("td")

            if len(cells) < 10:
                continue

            serial_no = nepali_to_roman_numerals(
                normalize_whitespace(cells[0].get_text())
            )
            division = self._clean_division(normalize_whitespace(cells[1].get_text()))
            registration_date = normalize_date(
                normalize_whitespace(cells[2].get_text())
            )
            bench_type = normalize_whitespace(cells[3].get_text())
            case_type = normalize_whitespace(cells[4].get_text())
            case_number = self._clean_case_number(
                normalize_whitespace(cells[5].get_text())
            )
            parties = normalize_whitespace(cells[6].get_text())
            judges_cannot_hear = self._parse_judges(cells[7])
            judges_must_hear = self._parse_judges(cells[8])
            remarks = normalize_whitespace(cells[9].get_text())  # कैफियत

            if not case_number:
                continue

            plaintiff = ""
            defendant = ""
            if "||" in parties:
                parts = parties.split("||", 1)
                plaintiff = normalize_whitespace(parts[0])
                defendant = normalize_whitespace(parts[1])
            else:
                raise ValueError(f"Unexpected parties format: {parties}, {date_bs}")
                plaintiff = parties

            case = self.case_cache.get(case_number, COURT_ID)
            if not case:
                case = CourtCase(
                    case_number=case_number,
                    court_identifier=COURT_ID,
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
                court_identifier=COURT_ID,
                hearing_date_bs=date_bs,
                hearing_date_ad=convert_bs_to_ad(date_bs),
                bench_type=bench_type,
                serial_no=serial_no,
                remarks=remarks,
                judge_names=judges_must_hear,
                scraped_at=datetime.now(KATHMANDU_TZ).replace(tzinfo=None),
                extra_data={
                    "judges_cannot_hear": judges_cannot_hear,
                    "judges_must_hear": judges_must_hear,
                },
            )

            data.append((case, hearing))

        return data

    def _save_cases_and_hearings(
        self, data: List[Tuple[CourtCase, CourtCaseHearing]], date_bs: str
    ):
        """Save cases and hearings in a transaction."""
        with self.session.begin():
            for case, hearing in data:
                self.session.merge(case)
                self.session.add(hearing)

            mark_date_scraped(self.session, COURT_ID, date_bs)

    def parse_cases(self, response):
        soup = BeautifulSoup(response.text, "html.parser")

        date_bs = response.meta["date_bs"]

        if (
            "The requested URL was rejected" in response.text
            or "support ID is:" in response.text
        ):
            self.logger.error(f"Request blocked by WAF for date {date_bs}")
            return

        case_table = self._find_case_table(soup)

        if not case_table:
            self.logger.warning(f"No case table found for date {date_bs}")
            self._save_cases_and_hearings([], date_bs)
            return

        rows = self._find_case_rows(case_table)

        if not rows:
            self.logger.info(f"No cases found for date BS {date_bs}")
            self._save_cases_and_hearings([], date_bs)
            return

        data = self._extract_case_data(rows, date_bs)
        self._save_cases_and_hearings(data, date_bs)

        self.logger.info(f"Saved {len(data)} cases for date BS {date_bs}")


if __name__ == "__main__":
    process = CrawlerProcess({"LOG_LEVEL": "INFO"})
    process.crawl(SupremeCourtCasesSpider)
    process.start()
