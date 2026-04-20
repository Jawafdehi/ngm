"""DataLoader - fetches case data from the NGM database and CSV files."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from ngm.database.models import CourtCase, CaseEntity, get_engine, get_session
from ngm.ciaa_dataset.constants import TARGET_CASE_NUMBERS

logger = logging.getLogger(__name__)


class DataLoader:
    """Loads data from NGM DB and CSV files."""

    def __init__(
        self,
        ag_index_path: str,
        press_releases_csv_path: str,
        punaravedan_csv_path: Optional[str] = None,
        database_url: Optional[str] = None,
    ):
        """
        Args:
            ag_index_path: Path to ag_index.csv.
            press_releases_csv_path: Path to ciaa-press-releases.csv.
            punaravedan_csv_path: Path to punaravedan.csv. Defaults to package-relative path.
            database_url: PostgreSQL connection string. Falls back to DATABASE_URL env var.
        """
        self.ag_index_path = Path(ag_index_path)
        self.press_releases_csv_path = Path(press_releases_csv_path)
        self.punaravedan_csv_path = (
            Path(punaravedan_csv_path)
            if punaravedan_csv_path
            else Path(__file__).parent / "data" / "punaravedan.csv"
        )
        self._engine = get_engine(database_url)

    def _session(self) -> Session:
        return get_session(self._engine)

    # ------------------------------------------------------------------
    # Court cases from DB
    # ------------------------------------------------------------------

    def load_special_court_cases(self, fiscal_year: int) -> list[CourtCase]:
        """
        Return Special Court cases for a given fiscal year.

        Uses constants.py allowlist if entries exist for the year, otherwise
        falls back to a DB prefix query (covers years not yet in constants.py).
        """
        fy_prefix = str(fiscal_year)[-2:]  # e.g. 2080 -> "80"
        prefix = f"0{fy_prefix}-CR-"
        target_cases = [cn for cn in TARGET_CASE_NUMBERS if cn.startswith(prefix)]

        session = self._session()
        try:
            with session.begin():
                if target_cases:
                    cases = (
                        session.query(CourtCase)
                        .filter(
                            CourtCase.court_identifier == "special",
                            CourtCase.case_number.in_(target_cases),
                        )
                        .all()
                    )
                    logger.info(
                        "Loaded %d Special Court cases from database (constants.py allowlist)",
                        len(cases),
                    )
                else:
                    logger.info(
                        "No constants.py entries for FY %d; querying DB by prefix '%s'",
                        fiscal_year,
                        prefix,
                    )
                    cases = (
                        session.query(CourtCase)
                        .filter(
                            CourtCase.court_identifier == "special",
                            CourtCase.case_number.like(f"{prefix}%"),
                        )
                        .order_by(CourtCase.case_number)
                        .all()
                    )
                    logger.info(
                        "Loaded %d Special Court cases from database (DB prefix query for FY %d)",
                        len(cases),
                        fiscal_year,
                    )

                session.expunge_all()
                return cases
        finally:
            session.close()

    def load_supreme_court_case(self, original_case_number: str) -> Optional[CourtCase]:
        """Look up a Supreme Court case by case number (for appeal resolution)."""
        session = self._session()
        try:
            with session.begin():
                case = (
                    session.query(CourtCase)
                    .filter(
                        CourtCase.court_identifier == "supreme",
                        CourtCase.case_number == original_case_number,
                    )
                    .first()
                )
                if case:
                    session.expunge(case)
                return case
        finally:
            session.close()

    def _load_entities(
        self, case_number: str, court_identifier: str, side: str
    ) -> list[dict]:
        """Load party records (defendants or plaintiffs) from court_case_entities."""
        session = self._session()
        try:
            with session.begin():
                entities = (
                    session.query(CaseEntity)
                    .filter(
                        CaseEntity.case_number == case_number,
                        CaseEntity.court_identifier == court_identifier,
                        CaseEntity.side == side,
                    )
                    .all()
                )
                result = []
                for e in entities:
                    # Split comma-separated names (some rows have multiple defendants in one field)
                    if "," in e.name:
                        result.extend(
                            {"name": n} for n in e.name.split(",") if n.strip()
                        )
                    else:
                        result.append({"name": e.name})
                return result
        finally:
            session.close()

    def load_defendants(self, case_number: str, court_identifier: str) -> list[dict]:
        """Load defendant records from court_case_entities."""
        return self._load_entities(case_number, court_identifier, "defendant")

    def load_plaintiffs(self, case_number: str, court_identifier: str) -> list[dict]:
        """Load plaintiff records from court_case_entities."""
        return self._load_entities(case_number, court_identifier, "plaintiff")

    # ------------------------------------------------------------------
    # CSV data sources
    # ------------------------------------------------------------------

    def load_press_release_index(self) -> list[dict]:
        """
        Load the CIAA press release index from CSV.

        CSV columns: press_id, publication_date, title, full_text, source_url
        """
        if not self.press_releases_csv_path.exists():
            logger.error(
                "Press releases CSV not found: %s", self.press_releases_csv_path
            )
            return []

        results = []
        seen: set[str] = set()
        with open(self.press_releases_csv_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                press_id_raw = (row.get("press_id") or "").strip()
                if not press_id_raw or press_id_raw in seen:
                    continue
                seen.add(press_id_raw)
                try:
                    press_id = int(press_id_raw)
                except ValueError:
                    continue
                results.append(
                    {
                        "press_id": press_id,
                        "title": (row.get("title") or "").strip(),
                        "publication_date": (row.get("publication_date") or "").strip(),
                        "source_url": (row.get("source_url") or "").strip(),
                        "full_text": (row.get("full_text") or "").strip(),
                    }
                )
        logger.info("Loaded %d press releases from CSV", len(results))
        return results

    def load_ag_index(self) -> dict[str, dict]:
        """
        Parse ag_index.csv into a dict keyed by case_number.

        Each value is a dict with keys: case_number, title, filing_date, pdf_url, court_office.
        """
        if not self.ag_index_path.exists():
            logger.warning("ag_index.csv not found at %s", self.ag_index_path)
            return {}

        index: dict[str, dict] = {}
        with open(self.ag_index_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                case_no = (row.get("case_number") or "").strip()
                if case_no:
                    index[case_no] = {
                        "case_number": case_no,
                        "title": (row.get("title") or "").strip(),
                        "filing_date": (row.get("filing_date") or "").strip(),
                        "pdf_url": (row.get("pdf_url") or "").strip(),
                        "court_office": (row.get("court_office") or "").strip(),
                    }
        logger.info("Loaded %d AG index entries", len(index))
        return index

    def load_punaravedan_index(self) -> dict[str, dict]:
        """
        Load Supreme Court appeal mappings from पुनरावेदन.csv.

        Returns dict keyed by Special Court case number with Supreme Court case info.
        """
        if not self.punaravedan_csv_path.exists():
            logger.warning("punaravedan.csv not found at %s", self.punaravedan_csv_path)
            return {}

        index: dict[str, dict] = {}
        with open(self.punaravedan_csv_path, encoding="utf-8") as f:
            # Skip first two lines (blank line and title row)
            for _ in range(2):
                if next(f, None) is None:
                    logger.warning(
                        "punaravedan.csv has fewer than 2 header lines; skipping"
                    )
                    return {}
            reader = csv.DictReader(f)
            for row in reader:
                # CSV columns are in Nepali
                special_case_raw = (row.get("विशेष_अदालतको_मुद्दा_नं") or "").strip()
                supreme_case = (row.get("सर्वोच्च_अदालतको_मुद्दा_नं") or "").strip()
                faisala_date = (row.get("विशेष_अदालतको_फैसला_मिति") or "").strip()
                appeal_decision_date = (
                    row.get("आयोगको_पुनरावेदन_गर्ने_निर्णय_मिति") or ""
                ).strip()
                appeal_filing_date = (row.get("पुनरावेदन_दर्ता_मिति") or "").strip()

                # Handle multiple case numbers (map all to the same appeal)
                if special_case_raw and supreme_case:
                    case_numbers = [
                        token.strip()
                        for token in special_case_raw.split()
                        if token.strip()
                    ]
                    if not case_numbers:
                        case_numbers = [special_case_raw]

                    for case_num in case_numbers:
                        index[case_num] = {
                            "supreme_case_number": supreme_case,
                            "faisala_date": faisala_date,
                            "appeal_decision_date": appeal_decision_date,
                            "appeal_filing_date": appeal_filing_date,
                        }

        logger.info("Loaded %d appeal mappings from punaravedan.csv", len(index))
        return index
