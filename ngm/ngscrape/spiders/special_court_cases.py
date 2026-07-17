"""
Special Court Cases Scraper

Scrapes daily case lists from the Special Court.
URL: https://supremecourt.gov.np/special/syspublic.php?d=reports&f=daily_public

Two-stage: discover bench types for a date, then fetch each bench's list. A date
is only saved once every bench resolves (success or error — see the base class's
bench errback).
"""

from scrapy.http import FormRequest
from bs4 import BeautifulSoup

from ngm.ngscrape.base_spiders import (
    BaseCourtCasesSpider,
    CourtCase,
    CourtCaseHearing,
    convert_bs_to_ad,
)
from ngm.ngscrape.constants import SCRAPE_LOOKBACK_DAYS_SPECIAL_COURT
from ngm.utils.normalizer import (
    normalize_whitespace,
    normalize_date,
    nepali_to_roman_numerals,
    fix_parenthesis_spacing,
)

COURT_ID = "special"


class SpecialCourtCasesSpider(BaseCourtCasesSpider):
    name = "special_court_cases"
    base_url = (
        "https://supremecourt.gov.np/special/syspublic.php?d=reports&f=daily_public"
    )

    def lookback_days(self):
        return SCRAPE_LOOKBACK_DAYS_SPECIAL_COURT

    def court_contexts(self):
        return [COURT_ID]

    def build_requests_for_date(self, court, ad_date, nepali_date, date_bs, today_bs):
        syy = str(nepali_date.year)
        smm = str(nepali_date.month).zfill(2)
        sdd = str(nepali_date.day).zfill(2)
        yield FormRequest(
            url=self.base_url,
            formdata={"mode": "showbench", "syy": syy, "smm": smm, "sdd": sdd},
            callback=self.parse_bench_types,
            meta={"date_bs": date_bs, "syy": syy, "smm": smm, "sdd": sdd},
            dont_filter=True,
        )

    def parse_bench_types(self, response):
        soup = BeautifulSoup(response.text, "html.parser")
        date_bs = response.meta["date_bs"]
        syy = response.meta["syy"]
        smm = response.meta["smm"]
        sdd = response.meta["sdd"]

        bench_select = soup.find("select", {"name": "bench_type"})
        if not bench_select:
            self.logger.info(f"No bench types found for date {date_bs}")
            self.save_cases([], COURT_ID, date_bs, "0 benches")
            return

        benches = [
            {"value": opt.get("value", "").strip(), "label": opt.get_text(strip=True)}
            for opt in bench_select.find_all("option")
            if opt.get("value", "").strip()
        ]
        if not benches:
            self.logger.info(f"No bench types found for date {date_bs}")
            self.save_cases([], COURT_ID, date_bs, "0 benches")
            return

        total = len(benches)
        note = f"{total} benches"
        self.logger.info(f"Found {total} bench types for date {date_bs}")

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
                errback=self.bench_errback,
                meta={
                    "court_key": COURT_ID,
                    "date_bs": date_bs,
                    "total_benches": total,
                    "bench_note": note,
                    "bench_type": bench["value"],
                    "bench_label": bench["label"],
                },
                dont_filter=True,
            )

    def parse_cases(self, response):
        soup = BeautifulSoup(response.text, "html.parser")
        meta = response.meta
        date_bs = meta["date_bs"]
        bench_type = meta["bench_type"]
        bench_label = meta["bench_label"]
        total = meta["total_benches"]
        note = meta["bench_note"]

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
            footer_text = normalize_whitespace(all_tables[-1].get_text())

        case_table = soup.find("table", {"width": "100%", "border": "1"})
        if not case_table:
            self.logger.warning(
                f"No case table found for bench {bench_type} on {date_bs}"
            )
            self.record_bench(COURT_ID, date_bs, total, [], note)
            return

        # judge_names is constant for the whole bench — compute once, not per row.
        judge_names = (
            "\n".join(
                normalize_whitespace(line)
                for line in judges_text.split("\n")
                if line.strip()
            )
            if judges_text
            else None
        )

        data = self.extract_rows(
            case_table.find_all("tr")[1:],
            date_bs=date_bs,
            hearing_date_ad=convert_bs_to_ad(date_bs),
            bench_type=bench_type,
            bench_label=bench_label,
            court_number=court_number,
            judge_names=judge_names,
            footer_text=footer_text,
        )
        self.logger.info(
            f"Extracted {len(data)} cases for bench {bench_type} on {date_bs}"
        )
        self.record_bench(COURT_ID, date_bs, total, data, note)

    def parse_row(
        self,
        row,
        cells,
        *,
        date_bs,
        hearing_date_ad,
        bench_type,
        bench_label,
        court_number,
        judge_names,
        footer_text,
    ):
        if len(cells) < 11:
            return None

        serial_no = nepali_to_roman_numerals(normalize_whitespace(cells[0].get_text()))
        category = normalize_whitespace(cells[1].get_text())
        registration_date = normalize_date(normalize_whitespace(cells[2].get_text()))
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
            return None

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
                # category / original_case_number → extra_data, not v2 columns.
                extra_data={
                    "category": category,
                    "original_case_number": original_case_number,
                },
            )
            self.case_cache.set(case)

        hearing = CourtCaseHearing(
            case_number=case_number,
            court_identifier=COURT_ID,
            hearing_date_bs=date_bs,
            hearing_date_ad=hearing_date_ad,
            bench_type=bench_type,
            serial_no=serial_no,
            judge_names=judge_names,
            case_status=case_status,
            decision_type=decision_type,
            remarks=remarks,
            scraped_at=self._now_ktm(),
            extra_data={
                "bench_label": normalize_whitespace(bench_label),
                "court_number": court_number,
                "footer": footer_text,
            },
        )
        return (case, hearing)
