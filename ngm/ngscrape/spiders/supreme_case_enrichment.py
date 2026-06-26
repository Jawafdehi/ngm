"""
Supreme Court Case Enrichment Spider

Enriches existing supreme court cases with detailed information from case detail
pages. Two-stage: search by case number, follow the detail link, parse, save.
URL: https://supremecourt.gov.np/lic/sys.php?d=reports&f=case_details
"""

from typing import List, Dict
from urllib.parse import parse_qs, urlparse

import scrapy
from scrapy.http import FormRequest
from bs4 import BeautifulSoup

from ngm.ngscrape.base_spiders import (
    BaseCaseEnrichmentSpider,
    CourtCase,
    convert_bs_to_ad,
)
from ngm.utils.normalizer import normalize_whitespace, normalize_date

COURT_ID = "supreme"


def extract_caseno(href: str):
    """Extract the ``caseno`` query param from a detail-link href.

    Uses a real query-string parse (exact key, no truncation) so a value that
    itself contains ``=`` survives intact and a substring like ``xcaseno=``
    can't match the wrong parameter.
    """
    if not href:
        return None
    values = parse_qs(urlparse(href).query).get("caseno")
    return values[0] if values else None


def _split_parties(text: str) -> List[str]:
    """Split party text into individual parties."""
    text = text.replace("समेत", "").strip()
    slash_parts = [p.strip() for p in text.split("/") if p.strip()]

    parties = []
    for part in slash_parts:
        comma_parts = [p.strip() for p in part.split(",") if p.strip()]
        if comma_parts:
            parties.extend(comma_parts)
        elif part:
            parties.append(part)

    return parties if parties else ([text] if text else [])


def parse_basic_info_table(soup: BeautifulSoup) -> Dict:
    """Extract basic case information from the main table."""
    data = {}
    tables = soup.find_all("table", class_="table-hover")
    if not tables:
        return data

    for row in tables[0].find_all("tr"):
        if row.find("th"):
            continue
        cells = row.find_all("td")
        if len(cells) == 4:
            for label_cell, value_cell in ((cells[0], cells[1]), (cells[2], cells[3])):
                label = normalize_whitespace(label_cell.get_text())
                value = normalize_whitespace(value_cell.get_text())
                if label and value:
                    _map_field(data, label.rstrip(":।.").strip(), value)
        elif len(cells) == 2:
            label = normalize_whitespace(cells[0].get_text())
            value = normalize_whitespace(cells[1].get_text())
            if label and value:
                _map_field(data, label.rstrip(":।.").strip(), value)

    return data


def _map_field(data: Dict, label: str, value: str):
    """Map Nepali labels to standardized field names."""
    if label in ["दर्ता नँ", "दर्ता नँ .", "रजिष्ट्रेशन नं"]:
        data["registration_number"] = value[:100]

    elif label in ["दर्ता मिती", "दर्ता मिति"]:
        data["registration_date_bs"] = normalize_date(value)
        if value:
            data["registration_date_ad"] = convert_bs_to_ad(normalize_date(value))

    elif label in ["मुद्दाको किसिम", "मुद्दा", "मुद्दाको बिषय"]:
        if "case_type" not in data:
            data["case_type"] = value[:200]
        if "case_subject" not in data:
            data["case_subject"] = value

    elif label in ["मुद्दाको स्थिती", "मुद्दाको स्थिति"]:
        data["case_status"] = value[:100]

    elif label in ["फैसला मिती", "फैसला मिति", "निर्णय मिति"]:
        # Don't store the "no verdict yet" sentinel as a fake date — leave NULL.
        if value and value != "**** ** **":
            data["verdict_date_bs"] = normalize_date(value)
            data["verdict_date_ad"] = convert_bs_to_ad(normalize_date(value))

    elif label in ["फैसला", "आदेश /फैसलाको किसिम"]:
        data["verdict_type"] = value[:100]

    elif label in ["फैसला गर्ने मा. न्यायाधीश", "न्यायाधीश"]:
        data["verdict_judge"] = value[:200]

    elif label in ["फाँट", "इजलास"]:
        data["division"] = value[:100]

    elif label in ["पेशी चढेको संख्या"]:
        data["hearing_count"] = value[:20]


def parse_parties(soup: BeautifulSoup) -> Dict[str, List[Dict]]:
    """Extract plaintiff and defendant information."""
    entities = {"plaintiffs": [], "defendants": []}
    tables = soup.find_all("table", class_="table-hover")
    if not tables:
        return entities

    def _collect(label, value):
        label = normalize_whitespace(label).rstrip(":।.").strip()
        value = normalize_whitespace(value)
        if not value:
            return
        if label in ["वादीहरु", "वादी"]:
            target = entities["plaintiffs"]
            skip = {"वादीहरु", "वादी"}
        elif label in ["प्रतिवादीहरु", "प्रतिवादी"]:
            target = entities["defendants"]
            skip = {"प्रतिवादीहरु", "प्रतिवादी"}
        else:
            return
        for party in _split_parties(value):
            if party and party not in skip:
                target.append({"name": party[:500], "address": None})

    for row in tables[0].find_all("tr"):
        cells = row.find_all("td")
        if len(cells) == 4:
            _collect(cells[0].get_text(), cells[1].get_text())
            _collect(cells[2].get_text(), cells[3].get_text())
        elif len(cells) == 2:
            _collect(cells[0].get_text(), cells[1].get_text())

    return entities


def parse_hearings_and_timeline(soup: BeautifulSoup) -> Dict[str, List[Dict]]:
    """Parse hearing schedule and timeline information."""
    data = {"hearings": [], "timeline": []}

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [
            normalize_whitespace(cell.get_text())
            for cell in header_row.find_all(["th", "td"])
        ]

        if any("सुनवाइ मिती" in h for h in headers) and any(
            "न्यायाधीश" in h for h in headers
        ):
            for row in table.find_all("tr")[1:]:
                cells = row.find_all("td")
                if len(cells) >= 2:
                    date = normalize_whitespace(cells[0].get_text())
                    judges = normalize_whitespace(cells[1].get_text())
                    if date and judges and date not in ["सुनवाइ मिती", "मिती"]:
                        entry = {
                            "date": normalize_date(date),
                            "judges": judges,
                            "type": "hearing",
                        }
                        if len(cells) >= 3:
                            status = normalize_whitespace(cells[2].get_text())
                            if status and status not in ["मुद्दाको स्थिती", "स्थिती"]:
                                entry["status"] = status
                        if len(cells) >= 4:
                            order_type = normalize_whitespace(cells[3].get_text())
                            if order_type and order_type not in [
                                "आदेश /फैसलाको किसिम",
                                "",
                            ]:
                                entry["order_type"] = order_type
                        data["hearings"].append(entry)

        elif any("तारेख मिती" in h for h in headers) and any(
            "विवरण" in h for h in headers
        ):
            for row in table.find_all("tr")[1:]:
                cells = row.find_all("td")
                if len(cells) >= 2:
                    date = normalize_whitespace(cells[0].get_text())
                    details = normalize_whitespace(cells[1].get_text())
                    if date and date not in ["तारेख मिती", "मिती"]:
                        entry = {
                            "date": normalize_date(date),
                            "details": details if details else None,
                        }
                        if len(cells) >= 3:
                            event_type = normalize_whitespace(cells[2].get_text())
                            if event_type and event_type not in ["तारेखको किसिम", ""]:
                                entry["type"] = event_type
                        if "type" not in entry:
                            entry["type"] = details if details else "पेशी तारेख"
                        data["timeline"].append(entry)

    return data


class SupremeCaseEnrichmentSpider(BaseCaseEnrichmentSpider):
    name = "supreme_case_enrichment"
    search_url = "https://supremecourt.gov.np/lic/sys.php?d=reports&f=case_details"

    def court_filter(self):
        return CourtCase.court_identifier == COURT_ID

    def build_request(self, case_number, court_identifier):
        return FormRequest(
            url=self.search_url,
            method="POST",
            formdata={
                "syy": "",
                "smm": "",
                "sdd": "",
                "mode": "show",
                "list": "list",
                "regno": case_number,
                "tyy": "",
                "tmm": "",
                "tdd": "",
            },
            callback=self.parse_search_results,
            meta={"case_number": case_number, "court_identifier": COURT_ID},
            dont_filter=True,
            errback=self.handle_error,
        )

    def parse_search_results(self, response):
        soup = BeautifulSoup(response.text, "html.parser")
        case_number = response.meta["case_number"]

        if (
            "The requested URL was rejected" in response.text
            or "support ID is:" in response.text
        ):
            self.logger.error(f"Request blocked by WAF for case {case_number}")
            return

        detail_link = soup.find(
            "a", href=lambda x: x and "mode=view" in x and "caseno=" in x
        )
        if not detail_link:
            # Leave the case pending (don't permanently fail) — a missing detail
            # link is often transient, and it is retried on the next run. Matches
            # the original behavior.
            self.logger.warning(f"Case {case_number} not found / no detail link")
            return

        caseno = extract_caseno(detail_link.get("href"))
        if not caseno:
            self.logger.error(f"Could not extract caseno for {case_number}")
            return

        detail_url = (
            "https://supremecourt.gov.np/lic/sys.php?d=reports&f=case_details"
            f"&num=1&mode=view&caseno={caseno}"
        )
        yield scrapy.Request(
            url=detail_url,
            callback=self.parse_case_detail,
            meta={"case_number": case_number, "court_identifier": COURT_ID},
            dont_filter=True,
            errback=self.handle_error,
        )

    def parse_case_detail(self, response):
        soup = BeautifulSoup(response.text, "html.parser")
        case_number = response.meta["case_number"]

        if "The requested URL was rejected" in response.text:
            self.logger.error(f"Detail page blocked by WAF for case {case_number}")
            return

        enrichment_data = parse_basic_info_table(soup)
        entities = parse_parties(soup)
        hearings_timeline = parse_hearings_and_timeline(soup)
        extra = {
            "enrichment_hearings": hearings_timeline.get("hearings", []),
            "enrichment_timeline": hearings_timeline.get("timeline", []),
        }

        self.save_enrichment(case_number, COURT_ID, enrichment_data, extra, entities)
        self.logger.info(
            f"Enriched case {case_number}: "
            f"{len(entities['plaintiffs'])} plaintiffs, "
            f"{len(entities['defendants'])} defendants"
        )
