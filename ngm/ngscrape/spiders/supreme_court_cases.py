"""
Supreme Court Cases Scraper

Scrapes the weekly supplementary cause list from the Supreme Court.
URL: https://supremecourt.gov.np/lic/sys.php?d=reports&f=weekly_suppli_public
"""

import re

from scrapy.crawler import CrawlerProcess
from scrapy.http import FormRequest
from bs4 import BeautifulSoup

from ngm.ngscrape.base_spiders import (
    BaseCourtCasesSpider,
    CourtCase,
    CourtCaseHearing,
    convert_bs_to_ad,
)
from ngm.ngscrape.constants import SCRAPE_LOOKBACK_DAYS_SUPREME_COURT
from ngm.utils.normalizer import (
    normalize_whitespace,
    normalize_date,
    nepali_to_roman_numerals,
)

COURT_ID = "supreme"


class SupremeCourtCasesSpider(BaseCourtCasesSpider):
    name = "supreme_court_cases"
    base_url = (
        "https://supremecourt.gov.np/lic/sys.php?d=reports&f=weekly_suppli_public"
    )

    def lookback_days(self):
        return SCRAPE_LOOKBACK_DAYS_SUPREME_COURT

    def court_contexts(self):
        return [COURT_ID]

    def build_requests_for_date(self, court, ad_date, nepali_date, date_bs, today_bs):
        syy = str(nepali_date.year)
        smm = str(nepali_date.month).zfill(2)
        sdd = str(nepali_date.day).zfill(2)
        yield FormRequest(
            url=self.base_url,
            formdata={"syy": syy, "smm": smm, "sdd": sdd, "mode": "show", "yo": "1"},
            headers={
                "Referer": "https://supremecourt.gov.np/",
                "Origin": "https://supremecourt.gov.np",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            callback=self.parse_cases,
            meta={"date_bs": date_bs, "syy": syy, "smm": smm, "sdd": sdd},
            dont_filter=True,
        )

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
                if rows and rows[0].get("bgcolor") == "#FFCC00":
                    header_row = rows[0]

            if header_row:
                header_text = header_row.get_text()
                if (
                    "क्र" in header_text
                    and "मुद्दा नं" in header_text
                    and "पक्ष" in header_text
                    and self._validate_case_table(table)
                ):
                    return table

        for table in all_tables:
            rows = table.find_all("tr")
            if not rows:
                continue
            cells = rows[0].find_all(["td", "th"])
            if len(cells) == 10 and self._validate_case_table(table):
                return table

        return None

    def _validate_case_table(self, table):
        if not table:
            return False
        rows = table.find_all("tr")
        if len(rows) < 2:
            return False
        header_cells = rows[0].find_all(["td", "th"])
        return len(header_cells) == 10

    def _find_case_rows(self, table):
        return table.find_all("tr", bgcolor="#ffffff")

    def _clean_case_number(self, case_number):
        if not case_number:
            return case_number
        return re.sub(r"\s*\([^)]*\)\s*", "", case_number).strip()

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
        """Parse judges from a cell, handling <br> tags. Newline-separated string."""
        if not cell:
            return None
        for br in cell.find_all("br"):
            br.replace_with("\n")
        judge_names = [
            normalize_whitespace(name)
            for name in cell.get_text().split("\n")
            if normalize_whitespace(name)
        ]
        return "\n".join(judge_names) if judge_names else None

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
            self.save_cases([], COURT_ID, date_bs)
            return

        rows = self._find_case_rows(case_table)
        if not rows:
            self.logger.info(f"No cases found for date BS {date_bs}")
            self.save_cases([], COURT_ID, date_bs)
            return

        hearing_date_ad = convert_bs_to_ad(date_bs)
        data = self.extract_rows(rows, date_bs=date_bs, hearing_date_ad=hearing_date_ad)
        self.save_cases(data, COURT_ID, date_bs)
        self.logger.info(f"Saved {len(data)} cases for date BS {date_bs}")

    def parse_row(self, row, cells, *, date_bs, hearing_date_ad):
        if len(cells) < 10:
            return None

        serial_no = nepali_to_roman_numerals(normalize_whitespace(cells[0].get_text()))
        division = self._clean_division(normalize_whitespace(cells[1].get_text()))
        registration_date = normalize_date(normalize_whitespace(cells[2].get_text()))
        bench_type = normalize_whitespace(cells[3].get_text())
        case_type = normalize_whitespace(cells[4].get_text())
        case_number = self._clean_case_number(normalize_whitespace(cells[5].get_text()))
        parties = normalize_whitespace(cells[6].get_text())
        judges_cannot_hear = self._parse_judges(cells[7])
        judges_must_hear = self._parse_judges(cells[8])
        remarks = normalize_whitespace(cells[9].get_text())

        if not case_number:
            return None

        plaintiff = ""
        defendant = ""
        if "||" in parties:
            parts = parties.split("||", 1)
            plaintiff = normalize_whitespace(parts[0])
            defendant = normalize_whitespace(parts[1])
        else:
            # Best-effort fallback: keep the row rather than aborting the whole
            # day (the old code raised here, discarding every case for the date).
            self.logger.warning(
                f"Unexpected parties format (no '||'): {parties!r} on {date_bs}"
            )
            plaintiff = parties

        case = self.case_cache.get(case_number, COURT_ID)
        if not case:
            case = CourtCase(
                case_number=case_number,
                court_identifier=COURT_ID,
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
            court_identifier=COURT_ID,
            hearing_date_bs=date_bs,
            hearing_date_ad=hearing_date_ad,
            bench_type=bench_type,
            serial_no=serial_no,
            remarks=remarks,
            judge_names=judges_must_hear,
            scraped_at=self._now_ktm(),
            extra_data={
                "judges_cannot_hear": judges_cannot_hear,
                "judges_must_hear": judges_must_hear,
            },
        )
        return (case, hearing)


if __name__ == "__main__":
    process = CrawlerProcess({"LOG_LEVEL": "INFO"})
    process.crawl(SupremeCourtCasesSpider)
    process.start()
