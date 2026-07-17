"""
Special Court Case Enrichment Spider

Enriches existing special court cases with detailed information from case detail
pages.
URL: https://supremecourt.gov.np/special/syspublic.php?d=reports&f=case_details
"""

from typing import List, Dict

from scrapy.http import FormRequest
from bs4 import BeautifulSoup

from ngm.ngscrape.base_spiders import (
    BaseCaseEnrichmentSpider,
    CourtCase,
    convert_bs_to_ad,
)
from ngm.utils.normalizer import normalize_whitespace, normalize_date
from ngm.utils.case_status_parser import apply_status

COURT_ID = "special"


def parse_hearing_table(table) -> List[Dict[str, str]]:
    """Parse hearing schedule table (पेशी को विवरण)."""
    hearings = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) >= 4:
            judge_cell = cells[1]
            for br in judge_cell.find_all("br"):
                br.replace_with("\n")
            judge_names = [
                normalize_whitespace(line)
                for line in judge_cell.get_text().split("\n")
                if line.strip()
            ]
            hearings.append(
                {
                    "hearing_date": normalize_date(
                        normalize_whitespace(cells[0].get_text())
                    ),
                    "judges": judge_names,
                    "case_status": normalize_whitespace(cells[2].get_text()),
                    "decision_type": normalize_whitespace(cells[3].get_text()),
                }
            )
    return hearings


def parse_pesi_tarekh_table(table) -> List[Dict[str, str]]:
    """Parse pesi tarekh table (पेशी तारेख)."""
    pesi_dates = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) >= 2:
            pesi_dates.append(
                {
                    "pesi_date": normalize_date(
                        normalize_whitespace(cells[0].get_text())
                    ),
                    "pesi_type": normalize_whitespace(cells[1].get_text()),
                }
            )
    return pesi_dates


def parse_sadharan_tarekh_table(table) -> List[Dict[str, str]]:
    """Parse sadharan tarekh table (साधारण तारेख)."""
    sadharan_dates = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) >= 2:
            sadharan_dates.append(
                {
                    "tarekh_date": normalize_date(
                        normalize_whitespace(cells[0].get_text())
                    ),
                    "tarekh_type": normalize_whitespace(cells[1].get_text()),
                }
            )
    return sadharan_dates


def parse_related_cases_table(table) -> List[Dict[str, str]]:
    """Parse related cases table (लगाब मुद्दाहरुको विवरण)."""
    related_cases = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) >= 6:
            related_cases.append(
                {
                    "case_number": normalize_whitespace(cells[0].get_text()),
                    "registration_date": normalize_date(
                        normalize_whitespace(cells[1].get_text())
                    ),
                    "case_type": normalize_whitespace(cells[2].get_text()),
                    "plaintiff": normalize_whitespace(cells[3].get_text()),
                    "defendant": normalize_whitespace(cells[4].get_text()),
                    "current_status": normalize_whitespace(cells[5].get_text()),
                }
            )
    return related_cases


class SpecialCaseEnrichmentSpider(BaseCaseEnrichmentSpider):
    name = "special_case_enrichment"
    base_url = (
        "https://supremecourt.gov.np/special/syspublic.php?d=reports&f=case_details"
    )

    def court_filter(self):
        return CourtCase.court_identifier == COURT_ID

    def build_request(self, case_number, court_identifier):
        return FormRequest(
            url=self.base_url,
            method="POST",
            formdata={
                "syy": "",
                "smm": "",
                "sdd": "",
                "mode": "show",
                "regno": case_number,
                "submit": " Search ",
            },
            callback=self.parse_case_detail,
            meta={"case_number": case_number, "court_identifier": COURT_ID},
            dont_filter=True,
            errback=self.handle_error,
        )

    def parse_case_detail(self, response):
        soup = BeautifulSoup(response.text, "html.parser")
        case_number = response.meta["case_number"]

        main_table = soup.find(
            "table",
            {"width": "100%", "border": "0", "cellspacing": "0", "cellpadding": "1"},
        )
        if not main_table:
            self.logger.warning(
                f"Case {case_number} not found or page structure unexpected"
            )
            return

        enrichment_data, entities, hearings_timeline = self._extract_case_data(soup)

        extra = {
            "enrichment_hearings": hearings_timeline.get("hearings", []),
            "enrichment_pesi_tarekh": hearings_timeline.get("pesi_tarekh", []),
            "enrichment_sadharan_tarekh": hearings_timeline.get("sadharan_tarekh", []),
            "enrichment_related_cases": hearings_timeline.get("related_cases", []),
        }
        if "plaintiff_advocates" in hearings_timeline:
            extra["plaintiff_advocates"] = hearings_timeline["plaintiff_advocates"]
        if "defendant_advocates" in hearings_timeline:
            extra["defendant_advocates"] = hearings_timeline["defendant_advocates"]

        self.save_enrichment(case_number, COURT_ID, enrichment_data, extra, entities)
        self.logger.info(
            f"Enriched case {case_number}: "
            f"{len(entities['plaintiffs'])} plaintiffs, "
            f"{len(entities['defendants'])} defendants"
        )

    def _extract_case_data(self, soup: BeautifulSoup):
        """Extract all case data from the detail page."""
        enrichment_data = {}
        entities = {"plaintiffs": [], "defendants": []}
        hearings_timeline = {
            "hearings": [],
            "pesi_tarekh": [],
            "sadharan_tarekh": [],
            "related_cases": [],
        }

        main_table = soup.find(
            "table",
            {"width": "100%", "border": "0", "cellspacing": "0", "cellpadding": "1"},
        )
        if not main_table:
            return enrichment_data, entities, hearings_timeline

        for row in main_table.find_all("tr"):
            cells = row.find_all("td")
            for i, cell in enumerate(cells):
                if "caption" not in cell.get("class", []):
                    continue
                label = normalize_whitespace(cell.get_text()).rstrip(":").strip()
                if i + 1 >= len(cells) or "caption" in cells[i + 1].get("class", []):
                    continue
                value = normalize_whitespace(cells[i + 1].get_text())

                if label == "दर्ता नँ .":
                    enrichment_data["registration_number"] = value[:100] or None
                elif label == "दर्ता मिती":
                    enrichment_data["registration_date_bs"] = normalize_date(value)
                    if value:
                        enrichment_data["registration_date_ad"] = convert_bs_to_ad(
                            normalize_date(value)
                        )
                elif label == "मुद्दाको किसिम":
                    enrichment_data["category"] = value[:100] or None
                elif label == "मुद्दा":
                    enrichment_data["case_type"] = value[:200] or None
                elif label == "फाँट":
                    enrichment_data["division"] = value[:100] or None
                elif label == "मुद्दाको स्थिती":
                    apply_status(enrichment_data, value)
                elif label == "वादीहरु":
                    for name in value.split(","):
                        name = name.strip()
                        if name:
                            entities["plaintiffs"].append(
                                {"name": name[:500], "address": None}
                            )
                elif label == "प्रतिवादीहरु":
                    for name in value.split(","):
                        name = name.strip()
                        if name:
                            entities["defendants"].append(
                                {"name": name[:500], "address": None}
                            )
                elif "वादी अधिवक्ता" in label:
                    if value:
                        hearings_timeline["plaintiff_advocates"] = value
                elif "प्रतिवादी अधिवक्ता" in label:
                    if value:
                        hearings_timeline["defendant_advocates"] = value

        self._extract_section(
            soup, "पेशी तारेख", "pesi_tarekh", parse_pesi_tarekh_table, hearings_timeline
        )
        self._extract_section(
            soup,
            "साधारण तारेख",
            "sadharan_tarekh",
            parse_sadharan_tarekh_table,
            hearings_timeline,
        )
        self._extract_section(
            soup,
            "लगाब मुद्दाहरुको विवरण",
            "related_cases",
            parse_related_cases_table,
            hearings_timeline,
        )
        self._extract_section(
            soup, "पेशी को विवरण", "hearings", parse_hearing_table, hearings_timeline
        )

        return enrichment_data, entities, hearings_timeline

    @staticmethod
    def _extract_section(soup, heading_text, key, parser, hearings_timeline):
        """Find a `utivtbl` table that follows a heading and parse it into `key`."""
        heading = soup.find(string=lambda x: x and heading_text in x)
        if not heading:
            return
        parent_row = heading.find_parent("tr")
        if not parent_row:
            return
        next_row = parent_row.find_next_sibling("tr")
        if not next_row:
            return
        table = next_row.find("table", class_="utivtbl")
        if table:
            hearings_timeline[key] = parser(table)
