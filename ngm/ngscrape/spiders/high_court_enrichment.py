"""High Court Case Enrichment Spider.

Enriches existing high court cases with detailed information from case detail
pages. Processes cases from the 18 high courts with status pending/NULL.
"""

from typing import Dict, List

from scrapy.http import FormRequest
from bs4 import BeautifulSoup

from ngm.ngscrape.base_spiders import (
    BaseCaseEnrichmentSpider,
    CourtCase,
    convert_bs_to_ad,
)
from ngm.utils.court_ids import HIGH_COURTS
from ngm.utils.normalizer import normalize_date, normalize_whitespace


class HighCourtEnrichmentSpider(BaseCaseEnrichmentSpider):
    name = "high_court_enrichment"

    def __init__(self, court=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        identifiers = [c["identifier"] for c in HIGH_COURTS]
        if court is not None and court not in identifiers:
            raise ValueError(
                f"Invalid court identifier: {court}. Valid: {', '.join(identifiers)}"
            )
        self.courts = [court] if court else identifiers
        self.success_count = 0
        self.failed_count = 0
        self.logger.info(f"Initialized for courts: {self.courts}")

    def court_filter(self):
        return CourtCase.court_identifier.in_(self.courts)

    def build_request(self, case_number, court_identifier):
        return FormRequest(
            url=f"https://supremecourt.gov.np/court/{court_identifier}/case_details",
            method="POST",
            formdata={"case_no": case_number},
            callback=self.parse_case_detail,
            meta={"court_identifier": court_identifier, "case_number": case_number},
            dont_filter=True,
            errback=self.handle_error,
        )

    def handle_error(self, failure):
        self.failed_count += 1
        super().handle_error(failure)

    def parse_case_detail(self, response):
        case_number = response.meta["case_number"]
        court_identifier = response.meta["court_identifier"]
        self.logger.info(f"Processing {case_number} from {court_identifier}")

        if "Request Rejected" in response.text:
            self.logger.error(f"WAF rejection for {case_number}")
            self.failed_count += 1
            self.mark_failed(case_number, court_identifier)
            return

        soup = BeautifulSoup(response.text, "html.parser")
        result = self._extract_enrichment_data(soup)
        entities = self._extract_entities(soup)
        hearings = self._extract_hearings(soup)
        core_fields = result["core_fields"]
        extra_data = result["extra_data"]

        # If structured entities couldn't be parsed, stash the raw party text.
        if not entities["plaintiffs"] and not entities["defendants"]:
            for row in soup.find_all("div", class_="row"):
                cols = row.find_all("div", class_="col-xs-6")
                if len(cols) != 2:
                    continue
                label_elem = cols[0].find("strong")
                value_elem = cols[1].find("p")
                if not label_elem or not value_elem:
                    continue
                label = normalize_whitespace(label_elem.get_text())
                value = normalize_whitespace(value_elem.get_text())
                if "वादी" in label and "प्रतिवादी" not in label and value:
                    extra_data["वादीहरु"] = value
                elif "प्रतिवादी" in label and value:
                    extra_data["प्रतिवादीहरु"] = value

        has_data = (
            core_fields
            or extra_data
            or entities["plaintiffs"]
            or entities["defendants"]
            or hearings
        )
        if not has_data:
            self.logger.warning(f"No data extracted for {case_number}")
            self.failed_count += 1
            self.mark_failed(case_number, court_identifier)
            return

        extra = {
            **extra_data,
            "source_url": response.url,
            "enrichment_hearings": hearings,
        }
        try:
            saved = self.save_enrichment(
                case_number, court_identifier, core_fields, extra, entities
            )
        except Exception:
            self.logger.exception(f"Failed to save enrichment for {case_number}")
            self.failed_count += 1
            self.mark_failed(case_number, court_identifier)
            return

        if saved:
            self.success_count += 1
            self.logger.info(f"Successfully enriched {case_number}")
        else:
            self.failed_count += 1
            self.mark_failed(case_number, court_identifier)

    def _extract_enrichment_data(self, soup: BeautifulSoup) -> Dict:
        core_fields = {}
        extra_data = {}

        for row in soup.find_all("div", class_="row"):
            cols = row.find_all("div", class_="col-xs-6")
            if len(cols) != 2:
                continue
            label_elem = cols[0].find("strong")
            value_elem = cols[1].find("p")
            if not label_elem or not value_elem:
                continue

            # Strip trailing ':' AND '.' — the portal labels carry a trailing dot
            # (e.g. "दर्ता नँ.") which previously dumped these into extra_data
            # under raw keys instead of the typed columns.
            label = normalize_whitespace(label_elem.get_text()).rstrip(":.").strip()
            value = normalize_whitespace(value_elem.get_text())
            if not value or value == "--":
                continue
            if "वादी" in label or "प्रतिवादी" in label:
                continue

            if label == "दर्ता नँ":
                core_fields["registration_number"] = value[:100]
            elif label in ("दर्ता मिति", "दर्ता मिती"):
                core_fields["registration_date_bs"] = normalize_date(value)
                try:
                    core_fields["registration_date_ad"] = convert_bs_to_ad(
                        normalize_date(value)
                    )
                except Exception:
                    self.logger.exception(
                        f"Failed to convert registration_date_bs: {value!r}"
                    )
            elif label == "मुद्दाको किसिम":
                core_fields["case_type"] = value[:200]
                extra_data["case_type_display"] = value
            elif label in ("मुद्दाको स्थिति", "मुद्दाको स्थिती"):
                core_fields["case_status"] = value[:100]
                extra_data["raw_status_display"] = value
            elif label in ("फैसला मिति", "फैसला मिती"):
                if value != "**** ** **":
                    core_fields["verdict_date_bs"] = normalize_date(value)
                    try:
                        core_fields["verdict_date_ad"] = convert_bs_to_ad(
                            normalize_date(value)
                        )
                    except Exception:
                        self.logger.exception(
                            f"Failed to convert verdict_date_bs: {value!r}"
                        )
            elif label == "फैसला गर्ने न्यायाधीश":
                core_fields["verdict_judge"] = value[:500]
            elif label == "रुजु मिती":
                extra_data["review_date"] = value
            elif label == "फाँटवाला":
                extra_data["division_officer"] = value
            elif label == "फाँट":
                extra_data["division"] = value
                core_fields["division"] = value[:100]
            elif label == "अदालत":
                extra_data["court_name"] = value
            else:
                extra_data[label.replace(" ", "_").replace(":", "")] = value

        return {"core_fields": core_fields, "extra_data": extra_data}

    def _extract_entities(self, soup: BeautifulSoup) -> Dict[str, List[Dict]]:
        entities = {"plaintiffs": [], "defendants": []}

        plaintiff_panel = None
        defendant_panel = None
        for panel in soup.find_all("div", class_="panel-heading"):
            text = panel.get_text()
            if plaintiff_panel is None and "वादीको विवरण" in text:
                plaintiff_panel = panel
            elif defendant_panel is None and "प्रतिवादीहरु" in text:
                defendant_panel = panel

        # Format 1: panel-based (with address columns). Each side is searched
        # independently, so a defendant panel is found even with no plaintiff panel.
        if plaintiff_panel or defendant_panel:
            if plaintiff_panel:
                self._parse_panel(plaintiff_panel, entities["plaintiffs"])
            if defendant_panel:
                self._parse_panel(defendant_panel, entities["defendants"])
            return entities

        # Format 2: simple row-based (वादीहरु / प्रतिवादीहरु in col-xs-6 rows).
        for row in soup.find_all("div", class_="row"):
            cols = row.find_all("div", class_="col-xs-6")
            if len(cols) != 2:
                continue
            label_elem = cols[0].find("strong")
            value_elem = cols[1].find("p")
            if not label_elem or not value_elem:
                continue
            label = normalize_whitespace(label_elem.get_text())
            value = normalize_whitespace(value_elem.get_text())
            if not value or value == "--":
                continue
            if "वादी" in label and "प्रतिवादी" not in label:
                target = entities["plaintiffs"]
            elif "प्रतिवादी" in label:
                target = entities["defendants"]
            else:
                continue
            names = value.split(",") if "," in value else value.split("/")
            for name in names:
                name = name.strip()
                if name:
                    target.append({"name": name[:500], "address": None})

        return entities

    @staticmethod
    def _parse_panel(panel, target):
        body = panel.find_next("div", class_="panel-body")
        if not body:
            return
        # Skip the header row (नाम / ठेगाना).
        for row in body.find_all("div", class_="row")[1:]:
            cols = row.find_all("div", recursive=False)
            if len(cols) < 2:
                continue
            name = normalize_whitespace(cols[0].get_text())
            address = normalize_whitespace(cols[1].get_text())
            if name and name != "नाम":
                target.append(
                    {
                        "name": name[:500],
                        "address": (
                            address[:500] if address and address.strip() else None
                        ),
                    }
                )

    def _extract_hearings(self, soup: BeautifulSoup) -> List[Dict]:
        hearings = []
        for table in soup.find_all("table", class_="table"):
            headers = [normalize_whitespace(h.get_text()) for h in table.find_all("th")]
            if "सुनवाइ" in " ".join(headers):
                for row in table.find_all("tr")[1:]:
                    cells = row.find_all("td")
                    if len(cells) >= 4:
                        hearings.append(
                            {
                                "hearing_date": normalize_date(
                                    normalize_whitespace(cells[0].get_text())
                                ),
                                "judges": normalize_whitespace(cells[1].get_text()),
                                "case_status": normalize_whitespace(
                                    cells[2].get_text()
                                ),
                                "decision_type": normalize_whitespace(
                                    cells[3].get_text()
                                ),
                            }
                        )
                break
        return hearings

    def closed(self, reason):
        self.logger.info("=" * 60)
        self.logger.info(f"Spider closed: {reason}")
        self.logger.info(
            f"Results: {self.success_count} enriched, {self.failed_count} failed"
        )
        self.logger.info("=" * 60)
        if hasattr(self, "session") and self.session:
            self.session.close()
