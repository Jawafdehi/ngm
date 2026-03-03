"""
Supreme Court Case Enrichment Spider

Enriches existing supreme court cases with detailed information from case detail pages.
Loops through all supreme court cases and enriches cases that need detailed information.

URL pattern: https://supremecourt.gov.np/lic/sys.php?d=reports&f=case_details
POST params: regno (case number), mode=show, list=list
"""

import scrapy
from datetime import datetime
from typing import List, Dict
from scrapy.http import FormRequest
from bs4 import BeautifulSoup
import pytz
from sqlalchemy import and_, or_
from sqlalchemy.orm.attributes import flag_modified
from ngm.utils.normalizer import normalize_whitespace, normalize_date
from ngm.database.models import get_engine, get_session, init_db, CourtCase, CaseEntity
from ngm.utils.db_helpers import convert_bs_to_ad

KATHMANDU_TZ = pytz.timezone("Asia/Kathmandu")
COURT_ID = "supreme"


def _split_parties(text: str) -> List[str]:
    """Split party text into individual parties"""
    # Remove 'समेत' (and others) suffix
    text = text.replace("समेत", "").strip()

    # Split by comma
    parties = [p.strip() for p in text.split(",") if p.strip()]

    # If no commas, return as single party
    if not parties:
        return [text] if text else []

    return parties


def parse_basic_info_table(soup: BeautifulSoup) -> Dict:
    """Extract basic case information from the main table."""
    data = {}

    # Find the main case details table
    tables = soup.find_all("table", class_="table-hover")
    if not tables:
        return data

    main_table = tables[0]
    rows = main_table.find_all("tr")

    for row in rows:
        # Skip header rows
        if row.find("th"):
            continue

        cells = row.find_all("td")

        # Handle rows with 4 cells (2 label-value pairs side by side)
        if len(cells) == 4:
            # First pair (cells 0 and 1)
            label1 = normalize_whitespace(cells[0].get_text())
            value1 = normalize_whitespace(cells[1].get_text())
            if label1 and value1:
                label1 = label1.rstrip(":।.").strip()
                _map_field(data, label1, value1)

            # Second pair (cells 2 and 3)
            label2 = normalize_whitespace(cells[2].get_text())
            value2 = normalize_whitespace(cells[3].get_text())
            if label2 and value2:
                label2 = label2.rstrip(":।.").strip()
                _map_field(data, label2, value2)

        # Handle rows with 2 cells (single label-value pair)
        elif len(cells) == 2:
            label = normalize_whitespace(cells[0].get_text())
            value = normalize_whitespace(cells[1].get_text())
            if label and value:
                label = label.rstrip(":।.").strip()
                _map_field(data, label, value)

    return data


def _map_field(data: Dict, label: str, value: str):
    """Map Nepali labels to standardized field names"""
    # Registration number
    if label in ["दर्ता नँ", "दर्ता नँ .", "रजिष्ट्रेशन नं"]:
        data["registration_number"] = value[:100]

    # Registration date
    elif label in ["दर्ता मिती", "दर्ता मिति"]:
        data["registration_date_bs"] = normalize_date(value)
        if value:
            data["registration_date_ad"] = convert_bs_to_ad(normalize_date(value))

    # Case type/subject
    elif label in ["मुद्दाको किसिम", "मुद्दा", "मुद्दाको बिषय"]:
        if "case_type" not in data:
            data["case_type"] = value[:100]
        if "case_subject" not in data:
            data["case_subject"] = value

    # Case status
    elif label in ["मुद्दाको स्थिती", "मुद्दाको स्थिति"]:
        data["case_status"] = value[:100]

    # Verdict/decision date
    elif label in ["फैसला मिती", "फैसला मिति", "निर्णय मिति"]:
        data["verdict_date_bs"] = normalize_date(value)
        if value and value != "**** ** **":
            data["verdict_date_ad"] = convert_bs_to_ad(normalize_date(value))

    # Verdict type
    elif label in ["फैसला", "आदेश /फैसलाको किसिम"]:
        data["verdict_type"] = value[:100]

    # Judge
    elif label in ["फैसला गर्ने मा. न्यायाधीश", "न्यायाधीश"]:
        data["verdict_judge"] = value[:200]

    # Division/bench
    elif label in ["फाँट", "इजलास"]:
        data["division"] = value[:100]

    # Hearing count
    elif label in ["पेशी चढेको संख्या"]:
        data["hearing_count"] = value[:20]


def parse_parties(soup: BeautifulSoup) -> Dict[str, List[Dict]]:
    """Extract plaintiff and defendant information."""
    entities = {"plaintiffs": [], "defendants": []}

    # Find the main case details table
    tables = soup.find_all("table", class_="table-hover")
    if not tables:
        return entities

    main_table = tables[0]
    rows = main_table.find_all("tr")

    for row in rows:
        cells = row.find_all("td")

        # Handle rows with 4 cells (2 label-value pairs side by side)
        if len(cells) == 4:
            # Check first pair
            label1 = normalize_whitespace(cells[0].get_text()).rstrip(":।.").strip()
            value1 = normalize_whitespace(cells[1].get_text())

            if label1 in ["वादीहरु", "वादी"] and value1:
                parties = _split_parties(value1)
                for party in parties:
                    if party and party not in ["वादीहरु", "वादी"]:
                        entities["plaintiffs"].append(
                            {"name": party[:500], "address": None}
                        )

            elif label1 in ["प्रतिवादीहरु", "प्रतिवादी"] and value1:
                parties = _split_parties(value1)
                for party in parties:
                    if party and party not in ["प्रतिवादीहरु", "प्रतिवादी"]:
                        entities["defendants"].append(
                            {"name": party[:500], "address": None}
                        )

            # Check second pair
            label2 = normalize_whitespace(cells[2].get_text()).rstrip(":।.").strip()
            value2 = normalize_whitespace(cells[3].get_text())

            if label2 in ["वादीहरु", "वादी"] and value2:
                parties = _split_parties(value2)
                for party in parties:
                    if party and party not in ["वादीहरु", "वादी"]:
                        entities["plaintiffs"].append(
                            {"name": party[:500], "address": None}
                        )

            elif label2 in ["प्रतिवादीहरु", "प्रतिवादी"] and value2:
                parties = _split_parties(value2)
                for party in parties:
                    if party and party not in ["प्रतिवादीहरु", "प्रतिवादी"]:
                        entities["defendants"].append(
                            {"name": party[:500], "address": None}
                        )

        # Handle rows with 2 cells (single label-value pair)
        elif len(cells) == 2:
            label = normalize_whitespace(cells[0].get_text()).rstrip(":।.").strip()
            value = normalize_whitespace(cells[1].get_text())

            if label in ["वादीहरु", "वादी"] and value:
                parties = _split_parties(value)
                for party in parties:
                    if party and party not in ["वादीहरु", "वादी"]:
                        entities["plaintiffs"].append(
                            {"name": party[:500], "address": None}
                        )

            elif label in ["प्रतिवादीहरु", "प्रतिवादी"] and value:
                parties = _split_parties(value)
                for party in parties:
                    if party and party not in ["प्रतिवादीहरु", "प्रतिवादी"]:
                        entities["defendants"].append(
                            {"name": party[:500], "address": None}
                        )

    return entities


def parse_hearings_and_timeline(soup: BeautifulSoup) -> Dict[str, List[Dict]]:
    """Parse hearing schedule and timeline information."""
    data = {"hearings": [], "timeline": []}

    # Find all tables and look for hearing/timeline tables
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue

        # Get headers from both th and td elements
        headers = []
        for cell in header_row.find_all(["th", "td"]):
            headers.append(normalize_whitespace(cell.get_text()))

        # Look for hearing history table (सुनवाइ मिती, न्यायाधीशहरू)
        if any("सुनवाइ मिती" in h for h in headers) and any(
            "न्यायाधीश" in h for h in headers
        ):
            rows = table.find_all("tr")[1:]  # Skip header row

            for row in rows:
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

                        # Add status if available
                        if len(cells) >= 3:
                            status = normalize_whitespace(cells[2].get_text())
                            if status and status not in ["मुद्दाको स्थिती", "स्थिती"]:
                                entry["status"] = status

                        # Add order type if available
                        if len(cells) >= 4:
                            order_type = normalize_whitespace(cells[3].get_text())
                            if order_type and order_type not in [
                                "आदेश /फैसलाको किसिम",
                                "",
                            ]:
                                entry["order_type"] = order_type

                        data["hearings"].append(entry)

        # Look for timeline table (तारेख मिती, विवरण)
        elif any("तारेख मिती" in h for h in headers) and any(
            "विवरण" in h for h in headers
        ):
            rows = table.find_all("tr")[1:]  # Skip header row

            for row in rows:
                cells = row.find_all("td")
                if len(cells) >= 2:
                    date = normalize_whitespace(cells[0].get_text())
                    details = normalize_whitespace(cells[1].get_text())

                    if date and date not in ["तारेख मिती", "मिती"]:
                        entry = {
                            "date": normalize_date(date),
                            "details": details if details else None,
                        }

                        # Add type from 3rd column if available
                        if len(cells) >= 3:
                            event_type = normalize_whitespace(cells[2].get_text())
                            if event_type and event_type not in ["तारेखको किसिम", ""]:
                                entry["type"] = event_type

                        if "type" not in entry:
                            entry["type"] = details if details else "पेशी तारेख"

                        data["timeline"].append(entry)

    return data


class SupremeCaseEnrichmentSpider(scrapy.Spider):
    name = "supreme_case_enrichment"
    search_url = "https://supremecourt.gov.np/lic/sys.php?d=reports&f=case_details"

    custom_settings = {
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 408, 429],
        "CONCURRENT_REQUESTS": 4,  # Be gentle with enrichment requests
        # "DOWNLOAD_DELAY": 3,  # 3 second delay between requests
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def start_requests(self):
        """Generate requests for cases that need enrichment"""
        self.engine = get_engine()
        init_db(self.engine)
        self.session = get_session(self.engine)

        # Query all supreme court cases that need enrichment
        with self.session.begin():
            cases_to_enrich = (
                self.session.query(CourtCase.case_number)
                .filter(
                    and_(
                        CourtCase.court_identifier == COURT_ID,
                        or_(
                            CourtCase.status == "pending",
                            CourtCase.status.is_(None),
                        ),
                    )
                )
                .order_by(CourtCase.registration_date_ad.desc().nullslast())
                .all()
            )

        if not cases_to_enrich:
            self.logger.info("No supreme court cases to enrich")
            return

        self.logger.info(f"Found {len(cases_to_enrich)} supreme court cases to enrich")

        # Generate requests for each case
        for (case_number,) in cases_to_enrich:
            yield FormRequest(
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

    def parse_search_results(self, response):
        """Parse search results and extract detail link"""
        soup = BeautifulSoup(response.text, "html.parser")
        case_number = response.meta["case_number"]

        # Check if blocked by WAF
        if (
            "The requested URL was rejected" in response.text
            or "support ID is:" in response.text
        ):
            self.logger.error(f"Request blocked by WAF for case {case_number}")
            return

        # Find the case detail link
        detail_link = soup.find(
            "a", href=lambda x: x and "mode=view" in x and "caseno=" in x
        )

        if not detail_link:
            self.logger.warning(
                f"Case {case_number} not found or no detail link available"
            )
            return

        # Extract caseno from the link
        href = detail_link.get("href")
        caseno = None
        for param in href.split("&"):
            if "caseno=" in param:
                caseno = param.split("=")[1]
                break

        if not caseno:
            self.logger.error(
                f"Could not extract caseno from detail link for {case_number}"
            )
            return

        # Fetch the detailed case information
        detail_url = f"https://supremecourt.gov.np/lic/sys.php?d=reports&f=case_details&num=1&mode=view&caseno={caseno}"

        yield scrapy.Request(
            url=detail_url,
            callback=self.parse_case_detail,
            meta={"case_number": case_number, "caseno": caseno},
            dont_filter=True,
            errback=self.handle_error,
        )

    def parse_case_detail(self, response):
        """Parse the case detail page and update database"""
        soup = BeautifulSoup(response.text, "html.parser")
        case_number = response.meta["case_number"]

        # Check if blocked by WAF
        if "The requested URL was rejected" in response.text:
            self.logger.error(f"Detail page blocked by WAF for case {case_number}")
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
        enrichment_data = parse_basic_info_table(soup)
        entities = parse_parties(soup)
        hearings_timeline = parse_hearings_and_timeline(soup)

        # Update database
        self._save_enrichment(case_number, enrichment_data, entities, hearings_timeline)

        self.logger.info(
            f"Enriched case {case_number}: "
            f"{len(entities['plaintiffs'])} plaintiffs, {len(entities['defendants'])} defendants"
        )

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
            case.extra_data["enrichment_timeline"] = hearings_timeline.get(
                "timeline", []
            )

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
