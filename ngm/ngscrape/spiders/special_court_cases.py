import scrapy
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
    fix_parenthesis_spacing,
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
    SCRAPE_LOOKBACK_DAYS_SPECIAL_COURT,
    SCRAPE_OFFSET_DAYS,
)

COURT_ID = "special"
KATHMANDU_TZ = pytz.timezone("Asia/Kathmandu")


class SpecialCourtCasesSpider(scrapy.Spider):
    name = "special_court_cases"
    base_url = (
        "https://supremecourt.gov.np/special/syspublic.php?d=reports&f=daily_public"
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
        self.bench_types_by_date = {}
        self._bench_counter = {}
        self._data_by_date = {}

    def start_requests(self):
        now_ktm = datetime.now(KATHMANDU_TZ)
        end_date = now_ktm.date() - timedelta(days=SCRAPE_OFFSET_DAYS)
        start_date = end_date - timedelta(days=SCRAPE_LOOKBACK_DAYS_SPECIAL_COURT)

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
                    formdata={"mode": "showbench", "syy": syy, "smm": smm, "sdd": sdd},
                    callback=self.parse_bench_types,
                    meta={"date_bs": date_bs, "syy": syy, "smm": smm, "sdd": sdd},
                    dont_filter=True,
                )
            except Exception as e:
                self.logger.error(f"Error converting date {current_date}: {e}")

            current_date -= timedelta(days=1)

    def parse_bench_types(self, response):
        soup = BeautifulSoup(response.text, "html.parser")

        date_bs = response.meta["date_bs"]
        syy = response.meta["syy"]
        smm = response.meta["smm"]
        sdd = response.meta["sdd"]

        bench_select = soup.find("select", {"name": "bench_type"})

        if not bench_select:
            self.logger.info(f"No bench types found for date {date_bs}")
            self._save_cases_and_hearings([], date_bs)
            return

        bench_options = bench_select.find_all("option")
        benches = []

        for option in bench_options:
            value = option.get("value", "").strip()
            label = option.get_text(strip=True)
            if value:
                benches.append({"value": value, "label": label})

        self.logger.info(f"Found {len(benches)} bench types for date {date_bs}")
        self.bench_types_by_date[date_bs] = len(benches)

        yo_input = soup.find("input", {"name": "yo", "type": "hidden"})
        yo_value = yo_input.get("value", "1") if yo_input else "1"

        for bench in benches:
            yield FormRequest(
                url=self.base_url,
                formdata={
                    "mode": "show",
                    "syy": syy,
                    "smm": smm,
                    "sdd": sdd,
                    "bench_type": bench["value"],
                    "yo": yo_value,
                },
                callback=self.parse_cases,
                meta={
                    "date_bs": date_bs,
                    "syy": syy,
                    "smm": smm,
                    "sdd": sdd,
                    "bench_type": bench["value"],
                    "bench_label": bench["label"],
                    "total_benches": len(benches),
                },
                dont_filter=True,
            )

    def _extract_case_data(
        self,
        rows,
        date_bs,
        bench_type,
        bench_label,
        court_number,
        judges_text,
        footer_text,
    ) -> List[Tuple[CourtCase, CourtCaseHearing]]:
        data: List[Tuple[CourtCase, CourtCaseHearing]] = []

        for row in rows:
            cells = row.find_all("td")

            if len(cells) < 11:
                continue

            serial_no = nepali_to_roman_numerals(
                normalize_whitespace(cells[0].get_text())
            )
            category = normalize_whitespace(cells[1].get_text())
            registration_date = normalize_date(
                normalize_whitespace(cells[2].get_text())
            )
            case_type = normalize_whitespace(cells[3].get_text())
            case_number = normalize_whitespace(cells[4].get_text())
            plaintiff = normalize_whitespace(cells[5].get_text())
            defendant = normalize_whitespace(cells[6].get_text())
            original_case_number = fix_parenthesis_spacing(
                normalize_whitespace(cells[7].get_text())
            )
            remarks = normalize_whitespace(cells[8].get_text())
            case_status = normalize_whitespace(cells[9].get_text())
            decision_type = normalize_whitespace(cells[10].get_text())

            if not case_number:
                continue

            judge_names = (
                "\n".join(
                    [
                        normalize_whitespace(line)
                        for line in judges_text.split("\n")
                        if line.strip()
                    ]
                )
                if judges_text
                else None
            )

            case = self.case_cache.get(case_number, COURT_ID)
            if not case:
                case = CourtCase(
                    case_number=case_number,
                    court_identifier=COURT_ID,
                    registration_date_bs=registration_date,
                    registration_date_ad=convert_bs_to_ad(registration_date),
                    case_type=case_type,
                    category=category,
                    plaintiff=plaintiff,
                    defendant=defendant,
                    original_case_number=original_case_number,
                )
                self.case_cache.set(case)

            hearing = CourtCaseHearing(
                case_number=case_number,
                court_identifier=COURT_ID,
                hearing_date_bs=date_bs,
                hearing_date_ad=convert_bs_to_ad(date_bs),
                bench_type=bench_type,
                serial_no=serial_no,
                judge_names=judge_names,
                case_status=case_status,
                decision_type=decision_type,
                remarks=remarks,
                scraped_at=datetime.now(KATHMANDU_TZ).replace(tzinfo=None),
                extra_data={
                    "bench_label": normalize_whitespace(bench_label),
                    "court_number": court_number,
                    "footer": footer_text,
                },
            )

            data.append((case, hearing))

        return data

    def _save_cases_and_hearings(
        self, data: List[Tuple[CourtCase, CourtCaseHearing]], date_bs: str
    ):
        with self.session.begin():
            for case, hearing in data:
                self.session.merge(case)
                self.session.add(hearing)

            bench_count = self.bench_types_by_date.get(date_bs, 0)
            mark_date_scraped(self.session, COURT_ID, date_bs, f"{bench_count} benches")

    def _handle_bench_completion(
        self,
        date_bs: str,
        total_benches: int,
        new_data: List[Tuple[CourtCase, CourtCaseHearing]],
    ):
        self._bench_counter[date_bs] = self._bench_counter.get(date_bs, 0) + 1

        if self._bench_counter[date_bs] >= total_benches:
            all_data = self._data_by_date.get(date_bs, [])
            all_data.extend(new_data)
            self._save_cases_and_hearings(all_data, date_bs)
            self.logger.info(f"Saved all cases for date {date_bs}")
            self._data_by_date.pop(date_bs, None)
        else:
            if date_bs not in self._data_by_date:
                self._data_by_date[date_bs] = []
            self._data_by_date[date_bs].extend(new_data)

    def parse_cases(self, response):
        soup = BeautifulSoup(response.text, "html.parser")

        date_bs = response.meta["date_bs"]
        bench_type = response.meta["bench_type"]
        bench_label = response.meta["bench_label"]
        total_benches = response.meta["total_benches"]

        court_number_elem = soup.find(
            "font", string=lambda x: x and "इजलास" in x and "नं" in x
        )
        court_number = (
            normalize_whitespace(court_number_elem.get_text())
            if court_number_elem
            else ""
        )

        judges_text = ""
        for font_tag in soup.find_all("font", {"size": "2"}):
            text = font_tag.get_text(strip=True)
            if "अध्यक्ष माननीय न्यायाधीश" in text or "सदस्य माननीय न्यायाधीश" in text:
                parent_td = font_tag.find_parent("td")
                if parent_td:
                    for br in parent_td.find_all("br"):
                        br.replace_with("\n")
                    judges_text = parent_td.get_text()
                    break

        footer_text = ""
        all_tables = soup.find_all("table", {"width": "100%", "border": "0"})
        if all_tables:
            footer_table = all_tables[-1]
            footer_text = normalize_whitespace(footer_table.get_text())

        case_table = soup.find("table", {"width": "100%", "border": "1"})

        if not case_table:
            self.logger.warning(
                f"No case table found for bench {bench_type} on {date_bs}"
            )
            self._handle_bench_completion(date_bs, total_benches, [])
            return

        rows = case_table.find_all("tr")[1:]
        data = self._extract_case_data(
            rows,
            date_bs,
            bench_type,
            bench_label,
            court_number,
            judges_text,
            footer_text,
        )

        self.logger.info(
            f"Extracted {len(data)} cases for bench {bench_type} on {date_bs}"
        )
        self._handle_bench_completion(date_bs, total_benches, data)
