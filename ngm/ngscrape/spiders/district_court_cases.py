"""
District Court Cases Scraper

Scrapes daily case lists (pesi) from all district courts in Nepal.
URL pattern: https://supremecourt.gov.np/weekly_dainik/pesi/daily/{district_id}
POST params: todays_date (BS), pesi_date (yyyy-mm-dd BS)
"""

from scrapy.crawler import CrawlerProcess
from scrapy.http import FormRequest
from bs4 import BeautifulSoup

from ngm.ngscrape.base_spiders import (
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
from ngm.utils.court_ids import DISTRICT_COURTS


class DistrictCourtCasesSpider(BaseCourtCasesSpider):
    name = "district_court_cases"
    base_url = "https://supremecourt.gov.np/weekly_dainik/pesi/daily/{district_id}"

    def court_contexts(self):
        return DISTRICT_COURTS

    def court_key(self, court):
        return court["code_name"]

    def build_requests_for_date(self, court, ad_date, nepali_date, date_bs, today_bs):
        yield FormRequest(
            url=self.base_url.format(district_id=court["district_id"]),
            method="POST",
            formdata={
                "todays_date": today_bs,
                "pesi_date": date_bs,
                "submit": "खोज्नु होस्",
            },
            callback=self.parse_daily_list,
            meta={
                "code_name": court["code_name"],
                "district_id": court["district_id"],
                "district_name": court["district"],
                "date_bs": date_bs,
            },
            dont_filter=True,
        )

    def parse_daily_list(self, response):
        """Parse the daily case list response."""
        soup = BeautifulSoup(response.text, "html.parser")
        code_name = response.meta["code_name"]
        date_bs = response.meta["date_bs"]

        error_div = soup.find("div", class_="alert_error")
        if error_div and "Causelist is not available" in error_div.get_text():
            self.logger.info(f"No cases for {code_name} on {date_bs}")
            self.save_cases([], code_name, date_bs)
            return

        case_tables = soup.find_all("table", {"border": "1", "class": "record_display"})
        if not case_tables:
            self.logger.info(f"No case tables found for {code_name} on {date_bs}")
            self.save_cases([], code_name, date_bs)
            return

        data = self._extract_case_data(case_tables, code_name, date_bs)
        self.save_cases(data, code_name, date_bs)
        self.logger.info(f"Saved {len(data)} cases for {code_name} on {date_bs}")

    def _extract_case_data(self, case_tables, code_name, date_bs):
        """Walk per-bench tables, attributing each to its sibling bench header."""
        hearing_date_ad = convert_bs_to_ad(date_bs)
        data = []
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

            data.extend(
                self.extract_rows(
                    table.find_all("tr"),
                    code_name=code_name,
                    date_bs=date_bs,
                    hearing_date_ad=hearing_date_ad,
                    bench=current_bench,
                    judge=current_judge,
                )
            )

        return data

    def parse_row(
        self, row, cells, *, code_name, date_bs, hearing_date_ad, bench, judge
    ):
        if len(cells) < 10 or row.find("th"):
            return None

        serial_no = nepali_to_roman_numerals(normalize_whitespace(cells[0].get_text()))

        case_parts = cells[1].get_text(separator="\n").strip().split("\n")
        case_number = (
            nepali_to_roman_numerals(normalize_whitespace(case_parts[0]))
            if case_parts
            else ""
        )
        if not case_number:
            return None

        case_id = (
            nepali_to_roman_numerals(normalize_whitespace(case_parts[1].strip("()")))
            if len(case_parts) > 1
            else ""
        )

        # Secondary case number (when mudda no is split across two lines).
        secondary_case_number = None
        if len(case_parts) >= 2:
            secondary_case_number = nepali_to_roman_numerals(
                normalize_whitespace(case_parts[-1].strip("()"))
            )

        reg_date_parts = cells[2].get_text(separator="\n").strip().split("\n")
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
        decision_type = normalize_whitespace(cells[9].get_text())[:200] or ""

        case = self.case_cache.get(case_number, code_name)
        if not case:
            extra_data = {}
            if secondary_case_number:
                extra_data["secondary_case_number"] = secondary_case_number
            # Low-value legacy fields → extra_data, not first-class v2 columns.
            extra_data["section"] = section
            extra_data["priority"] = priority
            extra_data["case_id"] = case_id

            case = CourtCase(
                case_number=case_number,
                court_identifier=code_name,
                registration_date_bs=registration_date,
                registration_date_ad=convert_bs_to_ad(registration_date),
                case_type=case_type,
                plaintiff=plaintiff,
                defendant=defendant,
                extra_data=extra_data if extra_data else None,
            )
            self.case_cache.set(case)

        hearing = CourtCaseHearing(
            case_number=case_number,
            court_identifier=code_name,
            hearing_date_bs=date_bs,
            hearing_date_ad=hearing_date_ad,
            bench=bench,
            judge_names=judge,
            serial_no=serial_no,
            decision_type=decision_type,
            remarks=remarks,
            scraped_at=self._now_ktm(),
        )
        return (case, hearing)


if __name__ == "__main__":
    process = CrawlerProcess({"LOG_LEVEL": "INFO"})
    process.crawl(DistrictCourtCasesSpider)
    process.start()
