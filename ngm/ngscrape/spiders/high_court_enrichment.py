"""
High Court Case Enrichment Spider

Enriches existing high court cases with detailed information from case detail pages.
Processes cases from all 18 high courts that have status='pending' or status=NULL.
"""

import os
import scrapy
from datetime import datetime
from typing import List, Dict
from scrapy.http import FormRequest
from bs4 import BeautifulSoup
import pytz
from sqlalchemy import and_
from sqlalchemy.orm.attributes import flag_modified
from ngm.utils.normalizer import normalize_whitespace, normalize_date
from ngm.utils.court_ids import HIGH_COURTS
from ngm.database.models import get_engine, get_session, init_db, CourtCase, CaseEntity
from ngm.utils.db_helpers import convert_bs_to_ad

KATHMANDU_TZ = pytz.timezone("Asia/Kathmandu")


class HighCourtEnrichmentSpider(scrapy.Spider):
    name = "high_court_enrichment"

    custom_settings = {
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 408, 429],
        "CONCURRENT_REQUESTS": 4,
    }

    def __init__(self, court=None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        court_identifiers = [c["identifier"] for c in HIGH_COURTS]
        self.courts = [court] if court in court_identifiers else court_identifiers

        self.success_count = 0
        self.failed_count = 0

        self.logger.info(f"Initialized for courts: {self.courts}")

    def start_requests(self):
        db_url = os.getenv("LOCAL_DATABASE_URL") or os.getenv("DATABASE_URL")
        if not db_url:
            self.logger.error("No database URL found")
            return

        self.engine = get_engine(db_url)
        init_db(self.engine)
        self.session = get_session(self.engine)

        cases = self._fetch_cases()
        if not cases:
            self.logger.info("No cases to enrich")
            return

        for case_number, court_identifier in cases:
            url = f"https://supremecourt.gov.np/court/{court_identifier}/case_details"

            yield FormRequest(
                url=url,
                method="POST",
                formdata={"case_no": case_number},
                callback=self.parse_case_detail,
                meta={"court_identifier": court_identifier, "case_number": case_number},
                dont_filter=True,
                errback=self.handle_error,
            )

    def _fetch_cases(self):
        with self.session.begin():
            cases = (
                self.session.query(CourtCase.case_number, CourtCase.court_identifier)
                .filter(
                    and_(
                        CourtCase.court_identifier.in_(self.courts),
                        CourtCase.status.in_(["pending", None]),
                    )
                )
                .order_by(CourtCase.registration_date_ad.desc().nullslast())
                .all()
            )

        self.logger.info(f"Fetched {len(cases)} cases for enrichment")
        return cases

    def handle_error(self, failure):
        case_number = failure.request.meta.get("case_number")
        court_identifier = failure.request.meta.get("court_identifier")

        self.logger.error(f"Request failed for {case_number}: {failure.value}")
        self.failed_count += 1
        self._mark_as_failed(case_number, court_identifier)

    def parse_case_detail(self, response):
        case_number = response.meta["case_number"]
        court_identifier = response.meta["court_identifier"]

        self.logger.info(f"Processing {case_number} from {court_identifier}")

        if "Request Rejected" in response.text:
            self.logger.error(f"WAF rejection for {case_number}")
            self.failed_count += 1
            self._mark_as_failed(case_number, court_identifier)
            return

        soup = BeautifulSoup(response.text, "html.parser")

        enrichment_result = self._extract_enrichment_data(soup)
        entities = self._extract_entities(soup)
        hearings = self._extract_hearings(soup)

        self.logger.info(
            f"Extracted: {len(enrichment_result['core_fields'])} core fields, "
            f"{len(enrichment_result['extra_data'])} extra fields, "
            f"{len(entities['plaintiffs'])} plaintiffs, "
            f"{len(entities['defendants'])} defendants, "
            f"{len(hearings)} hearings"
        )

        if not enrichment_result["core_fields"] and not enrichment_result["extra_data"]:
            self.logger.warning(f"No data extracted for {case_number}")
            self.failed_count += 1
            self._mark_as_failed(case_number, court_identifier)
            return

        self._save_enrichment(
            case_number,
            court_identifier,
            enrichment_result["core_fields"],
            enrichment_result["extra_data"],
            entities,
            hearings,
            response.url,
        )

        self.success_count += 1
        self.logger.info(f"Successfully enriched {case_number}")

    def _extract_enrichment_data(self, soup: BeautifulSoup) -> Dict:
        core_fields = {}
        extra_data = {}

        rows = soup.find_all("div", class_="row")

        for row in rows:
            cols = row.find_all("div", class_="col-xs-6")
            if len(cols) != 2:
                continue

            label_elem = cols[0].find("strong")
            value_elem = cols[1].find("p")

            if not label_elem or not value_elem:
                continue

            label = normalize_whitespace(label_elem.get_text()).rstrip(":").strip()
            value = normalize_whitespace(value_elem.get_text())

            if not value or value == "--":
                continue

            # Core database fields
            if label == "दर्ता नँ":
                core_fields["registration_number"] = value[:100]
            elif label == "दर्ता मिति":
                core_fields["registration_date_bs"] = normalize_date(value)
                try:
                    core_fields["registration_date_ad"] = convert_bs_to_ad(
                        normalize_date(value)
                    )
                except Exception:
                    pass
            elif label == "मुद्दाको किसिम":
                core_fields["case_type"] = value[:200]
                extra_data["case_type_display"] = value
            elif label == "मुद्दाको स्थिति":
                core_fields["case_status"] = value[:100]
                extra_data["raw_status_display"] = value
            elif label == "फैसला मिति":
                core_fields["verdict_date_bs"] = normalize_date(value)
                if value != "**** ** **":
                    try:
                        core_fields["verdict_date_ad"] = convert_bs_to_ad(
                            normalize_date(value)
                        )
                    except Exception:
                        pass
            elif label == "फैसला गर्ने न्यायाधीश":
                core_fields["verdict_judge"] = value[:500]

            # Extra data fields
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
                key = label.replace(" ", "_").replace(":", "")
                extra_data[key] = value

        return {"core_fields": core_fields, "extra_data": extra_data}

    def _extract_entities(self, soup: BeautifulSoup) -> Dict[str, List[Dict]]:
        entities = {"plaintiffs": [], "defendants": []}
        rows = soup.find_all("div", class_="row")

        for row in rows:
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
                entities["plaintiffs"].append({"name": value[:500], "address": None})
            elif "प्रतिवादी" in label:
                entities["defendants"].append({"name": value[:500], "address": None})

        return entities

    def _extract_hearings(self, soup: BeautifulSoup) -> List[Dict]:
        hearings = []
        tables = soup.find_all("table", class_="table")

        for table in tables:
            headers = [normalize_whitespace(h.get_text()) for h in table.find_all("th")]

            if "सुनवाइ" in " ".join(headers):
                rows = table.find_all("tr")[1:]

                for row in rows:
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

    def _save_enrichment(
        self,
        case_number,
        court_identifier,
        core_fields,
        extra_metadata,
        entities,
        hearings,
        source_url,
    ):
        now = datetime.now(KATHMANDU_TZ).replace(tzinfo=None)

        with self.session.begin():
            case = (
                self.session.query(CourtCase)
                .filter(
                    and_(
                        CourtCase.case_number == case_number,
                        CourtCase.court_identifier == court_identifier,
                    )
                )
                .first()
            )

            if not case:
                self.logger.error(f"Case {case_number} not found in database")
                self.failed_count += 1
                return

            # Update core fields
            for key, value in core_fields.items():
                setattr(case, key, value)

            self.logger.debug(f"Updated core fields: {list(core_fields.keys())}")

            # Update extra_data
            if case.extra_data is None:
                case.extra_data = {}

            extra_metadata["source_url"] = source_url
            extra_metadata["enrichment_hearings"] = hearings
            case.extra_data.update(extra_metadata)
            flag_modified(case, "extra_data")

            case.status = "enriched"
            case.updated_at = now

            # Update entities
            deleted = (
                self.session.query(CaseEntity)
                .filter(
                    and_(
                        CaseEntity.case_number == case_number,
                        CaseEntity.court_identifier == court_identifier,
                    )
                )
                .delete()
            )

            if deleted > 0:
                self.logger.debug(f"Deleted {deleted} old entities")

            for plaintiff in entities["plaintiffs"]:
                self.session.add(
                    CaseEntity(
                        case_number=case_number,
                        court_identifier=court_identifier,
                        side="plaintiff",
                        name=plaintiff["name"],
                        address=None,
                        created_at=now,
                        updated_at=now,
                    )
                )

            for defendant in entities["defendants"]:
                self.session.add(
                    CaseEntity(
                        case_number=case_number,
                        court_identifier=court_identifier,
                        side="defendant",
                        name=defendant["name"],
                        address=None,
                        created_at=now,
                        updated_at=now,
                    )
                )

            self.logger.debug(
                f"Saved {len(entities['plaintiffs'])} plaintiffs, "
                f"{len(entities['defendants'])} defendants"
            )

    def _mark_as_failed(self, case_number: str, court_identifier: str):
        try:
            with self.session.begin():
                case = (
                    self.session.query(CourtCase)
                    .filter(
                        and_(
                            CourtCase.case_number == case_number,
                            CourtCase.court_identifier == court_identifier,
                        )
                    )
                    .first()
                )

                if case:
                    case.status = "failed"
                    case.updated_at = datetime.now(KATHMANDU_TZ).replace(tzinfo=None)
        except Exception as e:
            self.logger.error(f"Failed to mark case as failed: {e}")

    def closed(self, reason):
        self.logger.info("=" * 60)
        self.logger.info(f"Spider closed: {reason}")
        self.logger.info(
            f"Results: {self.success_count} enriched, {self.failed_count} failed"
        )
        self.logger.info("=" * 60)

        if hasattr(self, "session") and self.session:
            self.session.close()
