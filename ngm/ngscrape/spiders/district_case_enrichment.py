"""
District Court Case Enrichment Spider

Enriches existing district court cases with detailed information from case detail
pages.
URL: https://supremecourt.gov.np/weekly_dainik/pesi/case_process_detail/{district_id}
POST params: mudda_no (case number in Devanagari), submit
"""

from typing import List, Dict

from scrapy.http import FormRequest
from bs4 import BeautifulSoup
from sqlalchemy import and_, or_

from ngm.ngscrape.base_spiders import (
    BaseCaseEnrichmentSpider,
    CourtCase,
    convert_bs_to_ad,
)
from ngm.utils.normalizer import normalize_date, roman_to_nepali_numerals
from ngm.utils.court_ids import DISTRICT_COURTS


def parse_party_table(table) -> List[Dict[str, str]]:
    """Parse a party (plaintiff/defendant) table."""
    parties = []
    for row in table.find_all("tr")[2:]:  # skip header rows
        cells = row.find_all("td")
        if len(cells) >= 2:
            name = cells[0].get_text(strip=True)
            address = cells[1].get_text(strip=True)
            if name:
                parties.append(
                    {
                        "name": name[:500],
                        "address": address[:500] if address else None,
                    }
                )
    return parties


def parse_hearing_table(table) -> List[Dict[str, str]]:
    """Parse hearing schedule table."""
    hearings = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) >= 5:
            hearings.append(
                {
                    "date": cells[0].get_text(strip=True),
                    "type": cells[1].get_text(strip=True),
                    "division": cells[2].get_text(strip=True),
                    "judge": cells[3].get_text(strip=True),
                    "order": cells[4].get_text(strip=True),
                }
            )
    return hearings


def parse_timeline_table(table) -> List[Dict[str, str]]:
    """Parse case timeline table."""
    timeline = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) >= 2:
            timeline.append(
                {
                    "date": cells[0].get_text(strip=True),
                    "type": cells[1].get_text(strip=True),
                }
            )
    return timeline


class DistrictCaseEnrichmentSpider(BaseCaseEnrichmentSpider):
    name = "district_case_enrichment"
    base_url = "https://supremecourt.gov.np/weekly_dainik/pesi/case_process_detail/{district_id}"

    custom_settings = {
        **BaseCaseEnrichmentSpider.custom_settings,
        "CONCURRENT_REQUESTS": 6,
    }

    def __init__(self, *args, backfill_case_type=False, **kwargs):
        super().__init__(*args, **kwargs)
        # Opt-in (`-a backfill_case_type=true`): also revisit already-enriched rows
        # missing a case_type (the cause-list lacks the detail page's मुद्दाको किसिम).
        self.backfill_case_type = str(backfill_case_type).lower() in (
            "1",
            "true",
            "yes",
        )
        self._court_lookup = {c["code_name"]: c for c in DISTRICT_COURTS}

    def court_filter(self):
        # Sargable membership test (replaces a leading-wildcard LIKE '%dc').
        return CourtCase.court_identifier.in_(list(self._court_lookup))

    def needs_enrichment_filter(self):
        base = or_(CourtCase.status == "pending", CourtCase.status.is_(None))
        if self.backfill_case_type:
            base = or_(
                base,
                and_(
                    CourtCase.status == "enriched",
                    or_(CourtCase.case_type.is_(None), CourtCase.case_type == ""),
                ),
            )
        return base

    def build_request(self, case_number, court_identifier):
        court_info = self._court_lookup.get(court_identifier)
        if not court_info:
            self.logger.warning(f"Court {court_identifier} not in DISTRICT_COURTS")
            return None
        return FormRequest(
            url=self.base_url.format(district_id=court_info["district_id"]),
            method="POST",
            formdata={
                "mudda_no": roman_to_nepali_numerals(case_number),
                "submit": "खोज्नु होस्",
            },
            callback=self.parse_case_detail,
            meta={
                "case_number": case_number,
                "court_identifier": court_identifier,
                "code_name": court_identifier,
            },
            dont_filter=True,
            errback=self.handle_error,
        )

    def parse_case_detail(self, response):
        soup = BeautifulSoup(response.text, "html.parser")
        code_name = response.meta["code_name"]
        case_number = response.meta["case_number"]

        if (
            "वादी/प्रतिवादीको विवरण" not in response.text
            and "पेशी विवरण" not in response.text
        ):
            self.logger.warning(f"Case {case_number} not found in detail page")
            self.mark_failed(case_number, code_name)
            return

        enrichment_data = self._extract_enrichment_data(soup)

        if self.backfill_case_type:
            # If a parallel worker already enriched this row, only backfill the
            # missing case_type (don't rebuild entities/hearings).
            with self.session.begin():
                case = self._get_case(case_number, code_name, lock=True)
                if case and case.status == "enriched":
                    if case.case_type:
                        return
                    case_type = enrichment_data.get("case_type")
                    if case_type:
                        case.case_type = case_type[:200]
                        case.updated_at = self._now_ktm()
                        self.logger.info(
                            f"Backfilled case_type for {case_number} ({code_name})"
                        )
                    return

        entities = self._extract_entities(soup)
        hearings_timeline = self._extract_hearings_timeline(soup)
        extra = {
            "enrichment_hearings": hearings_timeline.get("hearings", []),
            "enrichment_timeline": hearings_timeline.get("timeline", []),
        }

        self.save_enrichment(case_number, code_name, enrichment_data, extra, entities)
        self.logger.info(
            f"Enriched case {case_number} ({code_name}): "
            f"{len(entities['plaintiffs'])} plaintiffs, "
            f"{len(entities['defendants'])} defendants"
        )

    def _extract_enrichment_data(self, soup: BeautifulSoup) -> Dict:
        data = {}
        for content_div in soup.find_all("div", class_="content"):
            for dl in content_div.find_all("dl"):
                dts = dl.find_all("dt")
                dds = dl.find_all("dd")
                for dt, dd in zip(dts, dds):
                    label = dt.get_text(strip=True).rstrip(":").strip()
                    value = dd.get_text(strip=True)

                    if label == "रजिष्ट्रेशन नं" and value:
                        data["registration_number"] = value[:100]
                    elif label == "मुद्दाको किसिम" and value:
                        data["case_type"] = value[:200]
                    elif label == "मुद्दाको बिषय" and value:
                        data["case_subject"] = value
                    elif label == "मुद्दाको स्थिति" and value:
                        data["case_status"] = value[:100]
                    elif label == "फैसला मिति" and value and value != "**** ** **":
                        # Don't store the "no verdict" sentinel as a fake date.
                        data["verdict_date_bs"] = normalize_date(value)
                        data["verdict_date_ad"] = convert_bs_to_ad(
                            normalize_date(value)
                        )
                    elif label == "फैसला गर्ने मा. न्यायाधीश" and value:
                        data["verdict_judge"] = value[:200]
                    elif label == "पेशी चढेको संख्या" and value:
                        data["hearing_count"] = value[:20]

        if "registration_number" not in data:
            for h2 in soup.find_all("h2"):
                text = h2.get_text(strip=True)
                if "रजिष्ट्रेशन नं" in text:
                    reg_num = text.split(":")[-1].strip()
                    if reg_num:
                        data["registration_number"] = reg_num[:100]

        return data

    def _extract_entities(self, soup: BeautifulSoup) -> Dict[str, List[Dict]]:
        entities = {"plaintiffs": [], "defendants": []}

        h4_party = None
        for h4 in soup.find_all("h4"):
            if "वादी/प्रतिवादीको विवरण" in h4.get_text():
                h4_party = h4
                break
        if not h4_party:
            return entities

        parent_tr = h4_party.find_parent("tr")
        if not parent_tr:
            return entities
        next_tr = parent_tr.find_next_sibling("tr")
        if not next_tr:
            return entities

        for table in next_tr.find_all("table", class_="record_display"):
            header = table.find("th", colspan="2")
            if not header:
                continue
            header_text = header.get_text(strip=True)
            parties = parse_party_table(table)
            if "वादी" in header_text and "प्रतिवादी" not in header_text:
                entities["plaintiffs"] = parties
            elif "प्रतिवादी" in header_text:
                entities["defendants"] = parties

        return entities

    def _extract_hearings_timeline(self, soup: BeautifulSoup) -> Dict[str, List[Dict]]:
        data = {"hearings": [], "timeline": []}
        for h4 in soup.find_all("h4"):
            h4_text = h4.get_text(strip=True)
            parent = h4.find_parent("tr")
            if not parent:
                continue
            next_row = parent.find_next_sibling("tr")
            if not next_row:
                continue
            table = next_row.find("table", class_="record_display")
            if not table:
                continue
            if "पेशी विवरण" in h4_text:
                data["hearings"] = parse_hearing_table(table)
            elif "तारेख" in h4_text and "विवरण" in h4_text:
                data["timeline"] = parse_timeline_table(table)
        return data
