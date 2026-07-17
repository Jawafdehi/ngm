"""
High Court Cases Scraper

Scrapes daily case lists (cause lists) from all 18 high courts in Nepal.
URL pattern: https://supremecourt.gov.np/court/{court_id}/bench_list?pesi_date={date}

Two-stage: discover benches for a date, then fetch each bench's cause list. A
date is only saved once every bench resolves (success or error — see the base
class's bench errback), so a single failed bench can't strand the whole date.
"""

import re

import scrapy
from scrapy.http import FormRequest
from bs4 import BeautifulSoup

from ngm.ngscrape.base_spiders import (
    RETRY_SETTINGS,
    BaseCourtCasesSpider,
    CourtCase,
    CourtCaseHearing,
    convert_bs_to_ad,
)
from ngm.utils.normalizer import (
    normalize_whitespace,
    normalize_date,
    nepali_to_roman_numerals,
)
from ngm.utils.court_ids import HIGH_COURTS


class HighCourtCasesSpider(BaseCourtCasesSpider):
    name = "high_court_cases"

    custom_settings = {
        **RETRY_SETTINGS,
        "RETRY_PRIORITY_ADJUST": -1,
        "CONCURRENT_REQUESTS": 2,
    }

    def __init__(self, court=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        identifiers = [c["identifier"] for c in HIGH_COURTS]
        self.courts = [court] if (court and court in identifiers) else identifiers

    def court_contexts(self):
        return self.courts

    def build_requests_for_date(self, court, ad_date, nepali_date, date_bs, today_bs):
        pesi_date = (
            f"{nepali_date.year:04d}%2F{nepali_date.month:02d}%2F{nepali_date.day:02d}"
        )
        hearing_date = (
            f"{nepali_date.year:04d}{nepali_date.month:02d}{nepali_date.day:02d}"
        )
        yield scrapy.Request(
            url=f"https://supremecourt.gov.np/court/{court}/bench_list?pesi_date={pesi_date}",
            callback=self.parse_bench_list,
            meta={"court_id": court, "date_bs": date_bs, "hearing_date": hearing_date},
            dont_filter=True,
        )

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
            self.save_cases([], court_id, date_bs, "0 benches")
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
                    benches.append(
                        {
                            "bench_id": match.group(1),
                            "bench_no": match.group(2),
                            "judge_name": normalize_whitespace(cells[1].get_text()),
                        }
                    )

        if not benches:
            self.logger.info(f"No benches found for {court_id} - {date_bs}")
            self.save_cases([], court_id, date_bs, "0 benches")
            return

        total = len(benches)
        note = f"{total} benches"
        self.logger.info(f"Found {total} benches for {court_id} - {date_bs}")

        for bench in benches:
            yield FormRequest(
                url=f"https://supremecourt.gov.np/court/{court_id}/cause_list_detail",
                formdata={
                    "bench_id": bench["bench_id"],
                    "bench_no": bench["bench_no"],
                    "hearing_date": hearing_date,
                },
                callback=self.parse_cases,
                errback=self.bench_errback,
                meta={
                    "court_key": court_id,
                    "date_bs": date_bs,
                    "total_benches": total,
                    "bench_note": note,
                    "bench_id": bench["bench_id"],
                    "bench_no": bench["bench_no"],
                    "judge_name": bench["judge_name"],
                },
                dont_filter=True,
            )

    def parse_cases(self, response):
        soup = BeautifulSoup(response.text, "html.parser")
        meta = response.meta
        court_id = meta["court_key"]
        date_bs = meta["date_bs"]
        total = meta["total_benches"]
        note = meta["bench_note"]

        bench_type_elem = soup.find("h4", string=lambda x: x and "इजलास" in x)
        bench_type = (
            normalize_whitespace(bench_type_elem.get_text()) if bench_type_elem else ""
        )

        case_table = soup.find("table", class_="table table-bordered table-hover")
        if not case_table:
            self.logger.warning(
                f"No case table for {court_id} - bench {meta['bench_no']} on {date_bs}"
            )
            self.record_bench(court_id, date_bs, total, [], note)
            return

        rows = (
            case_table.find("tbody").find_all("tr", class_="data_row")
            if case_table.find("tbody")
            else []
        )
        if not rows:
            self.logger.info(
                f"No cases for {court_id} - bench {meta['bench_no']} on {date_bs}"
            )
            self.record_bench(court_id, date_bs, total, [], note)
            return

        data = self.extract_rows(
            rows,
            court_id=court_id,
            date_bs=date_bs,
            hearing_date_ad=convert_bs_to_ad(date_bs),
            bench_id=meta["bench_id"],
            bench_no=meta["bench_no"],
            bench_no_roman=nepali_to_roman_numerals(meta["bench_no"]),
            bench_type=bench_type,
            judge_name=meta["judge_name"],
        )
        self.logger.info(
            f"Extracted {len(data)} cases for {court_id} - "
            f"bench {meta['bench_no']} on {date_bs}"
        )
        self.record_bench(court_id, date_bs, total, data, note)

    def _clean_case_number(self, case_number_cell):
        for br in case_number_cell.find_all("br"):
            br.replace_with(" ")
        case_number = normalize_whitespace(case_number_cell.get_text())
        return re.sub(r"\s*\([^)]*\)\s*", "", case_number).strip()

    def parse_row(
        self,
        row,
        cells,
        *,
        court_id,
        date_bs,
        hearing_date_ad,
        bench_id,
        bench_no,
        bench_no_roman,
        bench_type,
        judge_name,
    ):
        if len(cells) < 9:
            return None

        serial_no = nepali_to_roman_numerals(normalize_whitespace(cells[0].get_text()))
        division = normalize_whitespace(cells[1].get_text())
        registration_date = normalize_date(normalize_whitespace(cells[2].get_text()))
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
            return None

        case = self.case_cache.get(case_number, court_id)
        if not case:
            case = CourtCase(
                case_number=case_number,
                court_identifier=court_id,
                registration_date_bs=registration_date,
                registration_date_ad=convert_bs_to_ad(registration_date),
                case_type=case_type,
                plaintiff=plaintiff,
                defendant=defendant,
                # division → extra_data, not a v2 column.
                extra_data={"division": division},
            )
            self.case_cache.set(case)

        hearing = CourtCaseHearing(
            case_number=case_number,
            court_identifier=court_id,
            hearing_date_bs=date_bs,
            hearing_date_ad=hearing_date_ad,
            bench=bench_no_roman,
            bench_type=bench_type,
            judge_names=judge_name,
            lawyer_names=lawyer_names,
            serial_no=serial_no,
            case_status=status,
            remarks=remarks,
            scraped_at=self._now_ktm(),
            extra_data={"bench_id": bench_id, "bench_no": bench_no},
        )
        return (case, hearing)
