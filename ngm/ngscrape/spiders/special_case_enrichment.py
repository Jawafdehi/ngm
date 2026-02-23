"""
Special Court Case Enrichment Spider

Enriches existing special court cases with detailed information from case detail pages.
Loops through all special court cases and enriches cases that need detailed information.

URL: https://supremecourt.gov.np/special/syspublic.php?d=reports&f=case_details
POST params: syy, smm, sdd, mode=show, regno (case number), submit
"""

import scrapy
from datetime import datetime
from typing import List, Dict
from scrapy.http import FormRequest
from bs4 import BeautifulSoup
import pytz
from sqlalchemy import and_
from sqlalchemy.orm.attributes import flag_modified
from ngm.utils.normalizer import (
    normalize_whitespace,
    normalize_date,
)
from ngm.database.models import get_engine, get_session, init_db, CourtCase, CaseEntity
from ngm.utils.db_helpers import convert_bs_to_ad

KATHMANDU_TZ = pytz.timezone("Asia/Kathmandu")
COURT_ID = "special"


def parse_hearing_table(table) -> List[Dict[str, str]]:
    """Parse hearing schedule table (पेशी को विवरण)."""
    hearings = []
    rows = table.find_all("tr")[1:]  # Skip header row

    for row in rows:
        cells = row.find_all("td")
        if len(cells) >= 4:
            # Extract judge names (may be multiple, separated by <br>)
            judge_cell = cells[1]
            for br in judge_cell.find_all("br"):
                br.replace_with("\n")
            judge_text = judge_cell.get_text()
            judge_names = [
                normalize_whitespace(line)
                for line in judge_text.split("\n")
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
    rows = table.find_all("tr")[1:]  # Skip header row

    for row in rows:
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
    rows = table.find_all("tr")[1:]  # Skip header row

    for row in rows:
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
    rows = table.find_all("tr")[1:]  # Skip header row

    for row in rows:
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


class SpecialCaseEnrichmentSpider(scrapy.Spider):
    name = "special_case_enrichment"
    base_url = (
        "https://supremecourt.gov.np/special/syspublic.php?d=reports&f=case_details"
    )

    custom_settings = {
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 408, 429],
        "CONCURRENT_REQUESTS": 4,  # Be gentle with enrichment requests
        # "DOWNLOAD_DELAY": 2,  # 2 second delay between requests
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def start_requests(self):
        """Generate requests for cases that need enrichment"""
        self.engine = get_engine()
        init_db(self.engine)
        self.session = get_session(self.engine)

        # Query all special court cases that need enrichment
        with self.session.begin():
            cases_to_enrich = (
                self.session.query(CourtCase.case_number)
                .filter(
                    and_(
                        CourtCase.court_identifier == COURT_ID,
                        CourtCase.status.in_(["pending", None]),
                    )
                )
                .order_by(CourtCase.registration_date_ad.desc().nullslast())
                .all()
            )

        if not cases_to_enrich:
            self.logger.info("No special court cases to enrich")
            return

        self.logger.info(f"Found {len(cases_to_enrich)} special court cases to enrich")

        # Generate requests for each case
        for (case_number,) in cases_to_enrich:
            yield FormRequest(
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
                meta={
                    "case_number": case_number,
                },
                dont_filter=True,
                errback=self.handle_error,
            )

    def handle_error(self, failure):
        """Handle request errors"""
        request = failure.request
        case_number = request.meta.get("case_number")

        self.logger.error(f"Error enriching case {case_number}: {failure.value}")

    def parse_case_detail(self, response):
        """Parse the case detail page and update database"""
        soup = BeautifulSoup(response.text, "html.parser")
        case_number = response.meta["case_number"]

        # Check if case was found - look for the main data table
        main_table = soup.find(
            "table",
            {"width": "100%", "border": "0", "cellspacing": "0", "cellpadding": "1"},
        )
        if not main_table:
            self.logger.warning(
                f"Case {case_number} not found or page structure unexpected"
            )
            return

        # Check if already enriched (by parallel worker)
        with self.session.begin():
            case = (
                self.session.query(CourtCase)
                .filter(
                    and_(
                        CourtCase.case_number == case_number,
                        CourtCase.court_identifier == COURT_ID,
                    )
                )
                .first()
            )

            if not case:
                self.logger.warning(f"Case {case_number} not found in database")
                return

            if case.status == "enriched":
                self.logger.info(f"Case {case_number} already enriched, skipping")
                return

        # Extract enrichment data
        enrichment_data, entities, hearings_timeline = self._extract_case_data(soup)

        # Update database
        self._save_enrichment(case_number, enrichment_data, entities, hearings_timeline)

        self.logger.info(
            f"Enriched case {case_number}: "
            f"{len(entities['plaintiffs'])} plaintiffs, {len(entities['defendants'])} defendants"
        )

    def _extract_case_data(self, soup: BeautifulSoup) -> tuple:
        """Extract all case data from the detail page"""
        # Initialize result dictionaries
        enrichment_data = {}
        entities = {"plaintiffs": [], "defendants": []}
        hearings_timeline = {
            "hearings": [],
            "pesi_tarekh": [],
            "sadharan_tarekh": [],
            "related_cases": [],
        }

        # Find the main table
        main_table = soup.find(
            "table",
            {"width": "100%", "border": "0", "cellspacing": "0", "cellpadding": "1"},
        )
        if not main_table:
            return enrichment_data, entities, hearings_timeline

        # Extract basic case information
        rows = main_table.find_all("tr")

        for row in rows:
            cells = row.find_all("td")

            # Process all cells in the row
            for i, cell in enumerate(cells):
                # Check if this is a caption (label) cell
                if "caption" in cell.get("class", []):
                    label = normalize_whitespace(cell.get_text()).rstrip(":").strip()

                    # Get the value from the next cell
                    if i + 1 < len(cells) and "caption" not in cells[i + 1].get(
                        "class", []
                    ):
                        value = normalize_whitespace(cells[i + 1].get_text())

                        # Map labels to CourtCase model fields
                        if label == "दर्ता नँ .":
                            enrichment_data["registration_number"] = (
                                value[:100] if value else None
                            )
                        elif label == "दर्ता मिती":
                            enrichment_data["registration_date_bs"] = normalize_date(
                                value
                            )
                            if value:
                                enrichment_data["registration_date_ad"] = (
                                    convert_bs_to_ad(normalize_date(value))
                                )
                        elif label == "मुद्दाको किसिम":
                            enrichment_data["category"] = value[:100] if value else None
                        elif label == "मुद्दा":
                            enrichment_data["case_type"] = (
                                value[:200] if value else None
                            )
                        elif label == "फाँट":
                            enrichment_data["division"] = value[:100] if value else None
                        elif label == "मुद्दाको स्थिती":
                            enrichment_data["case_status"] = (
                                value[:100] if value else None
                            )
                        elif label == "वादीहरु":
                            if value:
                                entities["plaintiffs"].append(
                                    {"name": value[:500], "address": None}
                                )
                        elif label == "प्रतिवादीहरु":
                            if value:
                                entities["defendants"].append(
                                    {"name": value[:500], "address": None}
                                )
                        elif "वादी अधिवक्ता" in label:
                            if value:
                                hearings_timeline["plaintiff_advocates"] = value
                        elif "प्रतिवादी अधिवक्ता" in label:
                            if value:
                                hearings_timeline["defendant_advocates"] = value

        # Extract pesi tarekh (पेशी तारेख)
        pesi_heading = soup.find(string=lambda x: x and "पेशी तारेख" in x)
        if pesi_heading:
            parent_row = pesi_heading.find_parent("tr")
            if parent_row:
                next_row = parent_row.find_next_sibling("tr")
                if next_row:
                    pesi_table = next_row.find("table", class_="utivtbl")
                    if pesi_table:
                        hearings_timeline["pesi_tarekh"] = parse_pesi_tarekh_table(
                            pesi_table
                        )

        # Extract sadharan tarekh (साधारण तारेख)
        sadharan_heading = soup.find(string=lambda x: x and "साधारण तारेख" in x)
        if sadharan_heading:
            parent_row = sadharan_heading.find_parent("tr")
            if parent_row:
                next_row = parent_row.find_next_sibling("tr")
                if next_row:
                    sadharan_table = next_row.find("table", class_="utivtbl")
                    if sadharan_table:
                        hearings_timeline["sadharan_tarekh"] = (
                            parse_sadharan_tarekh_table(sadharan_table)
                        )

        # Extract related cases (लगाब मुद्दाहरुको विवरण)
        related_heading = soup.find(string=lambda x: x and "लगाब मुद्दाहरुको विवरण" in x)
        if related_heading:
            parent_row = related_heading.find_parent("tr")
            if parent_row:
                next_row = parent_row.find_next_sibling("tr")
                if next_row:
                    related_table = next_row.find("table", class_="utivtbl")
                    if related_table:
                        hearings_timeline["related_cases"] = parse_related_cases_table(
                            related_table
                        )

        # Extract hearings (पेशी को विवरण)
        hearing_heading = soup.find(string=lambda x: x and "पेशी को विवरण" in x)
        if hearing_heading:
            parent_row = hearing_heading.find_parent("tr")
            if parent_row:
                next_row = parent_row.find_next_sibling("tr")
                if next_row:
                    hearing_table = next_row.find("table", class_="utivtbl")
                    if hearing_table:
                        hearings_timeline["hearings"] = parse_hearing_table(
                            hearing_table
                        )

        return enrichment_data, entities, hearings_timeline

    def _save_enrichment(
        self,
        case_number: str,
        enrichment_data: Dict,
        entities: Dict[str, List[Dict]],
        hearings_timeline: Dict[str, List[Dict]],
    ):
        """Save enrichment data and entities to database"""
        now = datetime.now(KATHMANDU_TZ).replace(tzinfo=None)

        with self.session.begin():
            # Update case with enrichment data
            case = (
                self.session.query(CourtCase)
                .filter(
                    and_(
                        CourtCase.case_number == case_number,
                        CourtCase.court_identifier == COURT_ID,
                    )
                )
                .first()
            )

            if not case:
                self.logger.error(f"Case {case_number} not found for enrichment")
                return

            # Update fields
            for key, value in enrichment_data.items():
                setattr(case, key, value)

            # Store hearings and timeline in extra_data
            if case.extra_data is None:
                case.extra_data = {}

            case.extra_data["enrichment_hearings"] = hearings_timeline.get(
                "hearings", []
            )
            case.extra_data["enrichment_pesi_tarekh"] = hearings_timeline.get(
                "pesi_tarekh", []
            )
            case.extra_data["enrichment_sadharan_tarekh"] = hearings_timeline.get(
                "sadharan_tarekh", []
            )
            case.extra_data["enrichment_related_cases"] = hearings_timeline.get(
                "related_cases", []
            )

            # Store advocate information if available
            if "plaintiff_advocates" in hearings_timeline:
                case.extra_data["plaintiff_advocates"] = hearings_timeline[
                    "plaintiff_advocates"
                ]
            if "defendant_advocates" in hearings_timeline:
                case.extra_data["defendant_advocates"] = hearings_timeline[
                    "defendant_advocates"
                ]

            # Mark extra_data as modified
            flag_modified(case, "extra_data")

            case.status = "enriched"
            case.enriched_at = now
            case.updated_at = now

            # Delete existing entities for this case
            self.session.query(CaseEntity).filter(
                and_(
                    CaseEntity.case_number == case_number,
                    CaseEntity.court_identifier == COURT_ID,
                )
            ).delete()

            # Add plaintiff entities
            for plaintiff in entities["plaintiffs"]:
                entity = CaseEntity(
                    case_number=case_number,
                    court_identifier=COURT_ID,
                    side="plaintiff",
                    name=plaintiff["name"],
                    address=plaintiff.get("address"),
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(entity)

            # Add defendant entities
            for defendant in entities["defendants"]:
                entity = CaseEntity(
                    case_number=case_number,
                    court_identifier=COURT_ID,
                    side="defendant",
                    name=defendant["name"],
                    address=defendant.get("address"),
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(entity)
