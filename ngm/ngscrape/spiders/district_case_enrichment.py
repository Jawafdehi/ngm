"""
District Court Case Enrichment Spider

Enriches existing district court cases with detailed information from case detail pages.
Loops through all district courts and enriches cases that need detailed information.

URL pattern: https://supremecourt.gov.np/weekly_dainik/pesi/case_process_detail/{district_id}
POST params: mudda_no (case number in Devanagari), submit
"""

import scrapy
from datetime import datetime
from typing import List, Dict
from scrapy.http import FormRequest
from bs4 import BeautifulSoup
import pytz
from sqlalchemy import and_, or_
from sqlalchemy.orm.attributes import flag_modified
from ngm.utils.normalizer import (
    normalize_date,
    roman_to_nepali_numerals,
)
from ngm.utils.court_ids import DISTRICT_COURTS
from ngm.database.models import get_engine, get_session, init_db, CourtCase, CaseEntity
from ngm.utils.db_helpers import convert_bs_to_ad

KATHMANDU_TZ = pytz.timezone("Asia/Kathmandu")


def parse_party_table(table) -> List[Dict[str, str]]:
    """Parse a party (plaintiff/defendant) table."""
    parties = []
    rows = table.find_all("tr")[2:]  # Skip header rows

    for row in rows:
        cells = row.find_all("td")
        if len(cells) >= 2:
            name = cells[0].get_text(strip=True)
            address = cells[1].get_text(strip=True)

            # Only add if name is not empty
            if name:
                parties.append(
                    {
                        "name": name[:500],  # Truncate to field limit
                        "address": address[:500] if address else None,
                    }
                )

    return parties


def parse_hearing_table(table) -> List[Dict[str, str]]:
    """Parse hearing schedule table."""
    hearings = []
    rows = table.find_all("tr")[1:]  # Skip header row

    for row in rows:
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
    rows = table.find_all("tr")[1:]  # Skip header row

    for row in rows:
        cells = row.find_all("td")
        if len(cells) >= 2:
            timeline.append(
                {
                    "date": cells[0].get_text(strip=True),
                    "type": cells[1].get_text(strip=True),
                }
            )

    return timeline


class DistrictCaseEnrichmentSpider(scrapy.Spider):
    name = "district_case_enrichment"
    base_url = "https://supremecourt.gov.np/weekly_dainik/pesi/case_process_detail/{district_id}"

    custom_settings = {
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 408, 429],
        # "RETRY_PRIORITY_ADJUST": -1,
        "CONCURRENT_REQUESTS": 6,  # Be gentle with enrichment requests
        # "DOWNLOAD_DELAY": 2,  # 2 second delay between requests
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def start_requests(self):
        """Generate requests for cases that need enrichment"""
        self.engine = get_engine()
        init_db(self.engine)
        self.session = get_session(self.engine)

        # Query all district court cases that need enrichment in one go
        # Priority: newer registration dates first, status = pending or NULL
        with self.session.begin():
            cases_to_enrich = (
                self.session.query(CourtCase.case_number, CourtCase.court_identifier)
                .filter(
                    and_(
                        CourtCase.court_identifier.like("%dc"),
                        or_(
                            CourtCase.status == "pending",
                            CourtCase.status.is_(None),
                            # Revisit already-enriched rows whose case_type never
                            # got populated: the daily cause-list only carries the
                            # (often blank) "मुद्दा विषय" subject column, so these
                            # need the detail page's "मुद्दाको किसिम" to backfill.
                            and_(
                                CourtCase.status == "enriched",
                                or_(
                                    CourtCase.case_type.is_(None),
                                    CourtCase.case_type == "",
                                ),
                            ),
                        ),
                    )
                )
                .order_by(CourtCase.registration_date_ad.desc().nullslast())
                .all()
            )

        if not cases_to_enrich:
            self.logger.info("No district court cases to enrich")
            return

        # Log summary statistics
        cases_by_court = {}
        for case_number, court_identifier in cases_to_enrich:
            cases_by_court[court_identifier] = (
                cases_by_court.get(court_identifier, 0) + 1
            )

        self.logger.info(
            f"Found {len(cases_to_enrich)} total district court cases to enrich "
            f"across {len(cases_by_court)} courts"
        )

        # Create a lookup map for court identifiers to district info
        court_lookup = {court["code_name"]: court for court in DISTRICT_COURTS}

        # Generate requests for each case
        for case_number, court_identifier in cases_to_enrich:
            court_info = court_lookup.get(court_identifier)
            if not court_info:
                self.logger.warning(
                    f"Court {court_identifier} not found in DISTRICT_COURTS lookup"
                )
                continue

            district_id = court_info["district_id"]
            district_name = court_info["district"]

            # Convert case number to Devanagari
            case_number_devanagari = roman_to_nepali_numerals(case_number)

            url = self.base_url.format(district_id=district_id)

            yield FormRequest(
                url=url,
                method="POST",
                formdata={"mudda_no": case_number_devanagari, "submit": "खोज्नु होस्"},
                callback=self.parse_case_detail,
                meta={
                    "code_name": court_identifier,
                    "district_id": district_id,
                    "district_name": district_name,
                    "case_number": case_number,
                    "case_number_devanagari": case_number_devanagari,
                },
                dont_filter=True,
                errback=self.handle_error,
            )

    def handle_error(self, failure):
        """Handle request errors"""
        request = failure.request
        case_number = request.meta.get("case_number")
        code_name = request.meta.get("code_name")

        self.logger.error(
            f"Error enriching case {case_number} ({code_name}): {failure.value}"
        )

        # Mark case as failed
        with self.session.begin():
            case = (
                self.session.query(CourtCase)
                .filter(
                    and_(
                        CourtCase.case_number == case_number,
                        CourtCase.court_identifier == code_name,
                    )
                )
                .first()
            )

            if case:
                case.status = "failed"
                case.updated_at = datetime.now(KATHMANDU_TZ).replace(tzinfo=None)

    def parse_case_detail(self, response):
        """Parse the case detail page and update database"""
        soup = BeautifulSoup(response.text, "html.parser")

        code_name = response.meta["code_name"]
        case_number = response.meta["case_number"]

        # Check if case was found
        if (
            "वादी/प्रतिवादीको विवरण" not in response.text
            and "पेशी विवरण" not in response.text
        ):
            self.logger.warning(f"Case {case_number} not found in detail page")

            # Mark as failed
            with self.session.begin():
                case = (
                    self.session.query(CourtCase)
                    .filter(
                        and_(
                            CourtCase.case_number == case_number,
                            CourtCase.court_identifier == code_name,
                        )
                    )
                    .first()
                )

                if case:
                    case.status = "failed"
                    case.updated_at = datetime.now(KATHMANDU_TZ).replace(tzinfo=None)

            return

        # Check if already enriched (by parallel worker)
        with self.session.begin():
            case = (
                self.session.query(CourtCase)
                .filter(
                    and_(
                        CourtCase.case_number == case_number,
                        CourtCase.court_identifier == code_name,
                    )
                )
                .first()
            )

            if not case:
                self.logger.warning(f"Case {case_number} not found in database")
                return

            already_enriched = case.status == "enriched"
            has_case_type = bool(case.case_type)

        if already_enriched:
            if has_case_type:
                self.logger.info(f"Case {case_number} already enriched, skipping")
                return
            # Already enriched but missing case_type: backfill just that field
            # from the detail page without re-touching entities/hearings (whose
            # rebuild deletes rows that may have downstream linkages).
            case_type = self._extract_enrichment_data(soup).get("case_type")
            if case_type:
                self._backfill_case_type(case_number, code_name, case_type)
                self.logger.info(
                    f"Backfilled case_type for {case_number} ({code_name}): {case_type}"
                )
            else:
                self.logger.info(
                    f"Case {case_number} ({code_name}) has no case_type on detail page"
                )
            return

        # Extract enrichment data
        enrichment_data = self._extract_enrichment_data(soup)
        entities = self._extract_entities(soup)
        hearings_timeline = self._extract_hearings_timeline(soup)

        # Update database
        self._save_enrichment(
            case_number, code_name, enrichment_data, entities, hearings_timeline
        )

        self.logger.info(
            f"Enriched case {case_number} ({code_name}): "
            f"{len(entities['plaintiffs'])} plaintiffs, {len(entities['defendants'])} defendants"
        )

    def _extract_enrichment_data(self, soup: BeautifulSoup) -> Dict:
        """Extract enrichment data from case detail page"""
        data = {}

        # Extract basic information from dl/dt/dd tags
        content_divs = soup.find_all("div", class_="content")
        for content_div in content_divs:
            dls = content_div.find_all("dl")
            for dl in dls:
                dts = dl.find_all("dt")
                dds = dl.find_all("dd")

                for dt, dd in zip(dts, dds):
                    label = dt.get_text(strip=True).rstrip(":").strip()
                    value = dd.get_text(strip=True)

                    # Map Nepali labels to database fields
                    if label == "रजिष्ट्रेशन नं" and value:
                        data["registration_number"] = value[:100]
                    elif label == "मुद्दाको किसिम" and value:
                        # True case type, only available on the detail page; the
                        # cause-list listing has no such column (it carries the
                        # subject), which is why district case_type is often blank.
                        data["case_type"] = value[:200]
                    elif label == "मुद्दाको बिषय" and value:
                        data["case_subject"] = value
                    elif label == "मुद्दाको स्थिति" and value:
                        data["case_status"] = value[:100]
                    elif label == "फैसला मिति" and value:
                        data["verdict_date_bs"] = normalize_date(value)
                        if value and value != "**** ** **":
                            data["verdict_date_ad"] = convert_bs_to_ad(
                                normalize_date(value)
                            )
                    elif label == "फैसला गर्ने मा. न्यायाधीश" and value:
                        data["verdict_judge"] = value[:200]
                    elif label == "पेशी चढेको संख्या" and value:
                        data["hearing_count"] = value[:20]

        # Extract registration number from h2 tags if not found
        if "registration_number" not in data:
            h2_tags = soup.find_all("h2")
            for h2 in h2_tags:
                text = h2.get_text(strip=True)
                if "रजिष्ट्रेशन नं" in text:
                    reg_num = text.split(":")[-1].strip()
                    if reg_num:
                        data["registration_number"] = reg_num[:100]

        return data

    def _extract_entities(self, soup: BeautifulSoup) -> Dict[str, List[Dict]]:
        """Extract plaintiff and defendant information"""
        entities = {"plaintiffs": [], "defendants": []}

        # Find the section with party details
        h4_party = None
        for h4 in soup.find_all("h4"):
            if "वादी/प्रतिवादीको विवरण" in h4.get_text():
                h4_party = h4
                break

        if not h4_party:
            return entities

        # Find the parent row and get the next row with tables
        parent_tr = h4_party.find_parent("tr")
        if not parent_tr:
            return entities

        next_tr = parent_tr.find_next_sibling("tr")
        if not next_tr:
            return entities

        # Get both tables (plaintiff and defendant side by side)
        tables = next_tr.find_all("table", class_="record_display")

        for table in tables:
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
        """Extract hearing and timeline information"""
        data = {"hearings": [], "timeline": []}

        # Find hearing and timeline sections
        h4_tags = soup.find_all("h4")
        for h4 in h4_tags:
            h4_text = h4.get_text(strip=True)

            if "पेशी विवरण" in h4_text:
                # Find the table after this h4
                parent = h4.find_parent("tr")
                if parent:
                    next_row = parent.find_next_sibling("tr")
                    if next_row:
                        table = next_row.find("table", class_="record_display")
                        if table:
                            data["hearings"] = parse_hearing_table(table)

            elif "तारेख" in h4_text and "विवरण" in h4_text:
                # Find the table after this h4
                parent = h4.find_parent("tr")
                if parent:
                    next_row = parent.find_next_sibling("tr")
                    if next_row:
                        table = next_row.find("table", class_="record_display")
                        if table:
                            data["timeline"] = parse_timeline_table(table)

        return data

    def _backfill_case_type(self, case_number: str, code_name: str, case_type: str):
        """Set case_type on an already-enriched case, leaving everything else."""
        now = datetime.now(KATHMANDU_TZ).replace(tzinfo=None)

        with self.session.begin():
            case = (
                self.session.query(CourtCase)
                .filter(
                    and_(
                        CourtCase.case_number == case_number,
                        CourtCase.court_identifier == code_name,
                    )
                )
                .first()
            )

            if case and not case.case_type:
                case.case_type = case_type[:200]
                case.updated_at = now

    def _save_enrichment(
        self,
        case_number: str,
        code_name: str,
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
                        CourtCase.court_identifier == code_name,
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
            case.extra_data["enrichment_timeline"] = hearings_timeline.get(
                "timeline", []
            )

            # Mark extra_data as modified so SQLAlchemy persists the changes
            flag_modified(case, "extra_data")

            case.status = "enriched"
            case.enriched_at = now
            case.updated_at = now

            # Delete existing entities for this case (in case of re-enrichment)
            # NOTE: THIS IS A BIG RISK, AS WE MAY HAVE DOWNSTREAM LINKAGES OF ENRICHED CASE ENTITIES
            # TODO: Revisit this logic.
            self.session.query(CaseEntity).filter(
                and_(
                    CaseEntity.case_number == case_number,
                    CaseEntity.court_identifier == code_name,
                )
            ).delete()

            # Add plaintiff entities
            for plaintiff in entities["plaintiffs"]:
                entity = CaseEntity(
                    case_number=case_number,
                    court_identifier=code_name,
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
                    court_identifier=code_name,
                    side="defendant",
                    name=defendant["name"],
                    address=defendant.get("address"),
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(entity)
