"""Shared base classes for the NGM court-data spiders.

Two spider families historically duplicated ~50-65% of their bodies:

- **Listing** spiders (``*_court_cases``) walk a BS date range, POST a daily
  causelist, parse it into ``CourtCase`` + ``CourtCaseHearing`` rows, save, and
  mark the date scraped. Some courts (high, special) fan out per *bench* and only
  finalise a date once every bench has resolved.
- **Enrichment** spiders (``*_case_enrichment``) select ``CourtCase`` rows with
  status pending/NULL, POST a detail page per case, and UPDATE the case in place
  (core fields + ``extra_data``) plus rebuild its ``CaseEntity`` rows.

``BaseScrapeSpider`` holds what both share (DB/session lifecycle, retry settings,
a Kathmandu timestamp helper). ``BaseCourtCasesSpider`` and
``BaseCaseEnrichmentSpider`` hold the per-family skeletons; each concrete court
implements only the bits that genuinely differ (request shape, row/field
mapping). Court-specific HTML parsing is intentionally left in the subclasses.
"""

import re
from datetime import datetime, timedelta

import pytz
import scrapy
from nepali.datetime import nepalidate
from sqlalchemy import and_, or_
from sqlalchemy.orm.attributes import flag_modified

from ngm.database.models import (
    CaseEntity,
    CourtCase,
    CourtCaseHearing,
    get_engine,
    get_session,
    init_db,
)
from ngm.ngscrape.constants import SCRAPE_LOOKBACK_DAYS, SCRAPE_OFFSET_DAYS
from ngm.utils.db_helpers import (
    CaseCache,
    convert_bs_to_ad,
    get_scraped_dates,
    mark_date_scraped,
)

KATHMANDU_TZ = pytz.timezone("Asia/Kathmandu")

# Low-value legacy fields the v2 ORM (Django court_cases, migration 0003) does not
# project as columns; they are stashed in ``extra_data`` instead. Any that slip into
# an enrichment's core_fields are rerouted there rather than setattr-ing a dropped
# column (which would silently lose the value).
_LEGACY_EXTRA_FIELDS = (
    "division",
    "category",
    "section",
    "priority",
    "case_id",
    "original_case_number",
)

# Shared retry policy. Subclasses compose this with their own tweaks, e.g.
# ``custom_settings = {**RETRY_SETTINGS, "CONCURRENT_REQUESTS": 4}`` — Scrapy does
# not merge ``custom_settings`` across the class hierarchy, so the spread is
# explicit on purpose.
RETRY_SETTINGS = {
    "RETRY_ENABLED": True,
    "RETRY_TIMES": 3,
    "RETRY_HTTP_CODES": [500, 502, 503, 504, 408, 429],
}

# Header-label artifacts that occasionally get parsed as a party name.
_ENTITY_LABELS = {
    "नाम",
    "ठेगाना",
    "वादी",
    "प्रतिवादी",
    "वादीहरु",
    "प्रतिवादीहरु",
    "वादीको विवरण",
    "प्रतिवादीको विवरण",
}
# A bare 1-3 char run of digits/punctuation is a "no party" placeholder, not a name.
_PLACEHOLDER_RE = re.compile(r"^[0-9०-९.\-]{1,3}$")


def clean_parties(parties):
    """Strip names, drop header-label / placeholder rows, and dedupe by name.

    Centralises the entity fixes across all enrichment spiders: header cells
    parsed as names, whitespace-padded names hitting the 500-char cap, ``-``/
    ``.``/``1`` placeholders for missing parties, and the same party inserted
    once per hearing sub-row (dedup).
    """
    seen = set()
    cleaned = []
    for party in parties:
        name = (party.get("name") or "").strip()
        if not name or name in _ENTITY_LABELS or _PLACEHOLDER_RE.match(name):
            continue
        if name in seen:
            continue
        seen.add(name)
        cleaned.append({"name": name[:500], "address": party.get("address")})
    return cleaned


class BaseScrapeSpider(scrapy.Spider):
    """Common DB/session lifecycle + timestamp helper for all NGM spiders."""

    def _init_db(self, database_url=None):
        """Create the engine, ensure tables exist, open a session."""
        self.engine = get_engine(database_url)
        init_db(self.engine)
        self.session = get_session(self.engine)

    @staticmethod
    def _now_ktm():
        """Naive Kathmandu-local ``datetime`` (matches the stored audit columns)."""
        return datetime.now(KATHMANDU_TZ).replace(tzinfo=None)

    @staticmethod
    def _format_bs(nepali_date) -> str:
        """Render a ``nepalidate`` as the canonical ``YYYY-MM-DD`` BS string."""
        return f"{nepali_date.year:04d}-{nepali_date.month:02d}-{nepali_date.day:02d}"


class BaseCourtCasesSpider(BaseScrapeSpider):
    """Daily-causelist listing spiders.

    Owns the date-range loop, the scraped-date skip, row extraction (with a
    per-row guard so one malformed row can never sink a whole day), the save, and
    — for the per-bench courts — bench accumulation with an errback so a single
    failed bench still lets the date finalise.

    Subclasses implement: :meth:`court_contexts`, :meth:`court_key`,
    :meth:`build_requests_for_date`, :meth:`parse_row`, and optionally
    :meth:`lookback_days`.
    """

    custom_settings = {**RETRY_SETTINGS, "RETRY_PRIORITY_ADJUST": -1}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_db()
        self.case_cache = CaseCache()
        # Per-bench accumulation state, keyed by (court_key, date_bs). Only the
        # two-stage courts (high, special) touch these.
        self._bench_counter = {}
        self._data_by_date = {}

    # --- hooks subclasses implement -------------------------------------

    def lookback_days(self) -> int:
        """How many days back to scrape (override per court)."""
        return SCRAPE_LOOKBACK_DAYS

    def court_contexts(self):
        """Return the courts to scrape (opaque per-court objects)."""
        raise NotImplementedError

    def court_key(self, court) -> str:
        """Map a court context to its ``court_identifier`` (used as DB key)."""
        return court

    def build_requests_for_date(self, court, ad_date, nepali_date, date_bs, today_bs):
        """Yield the request(s) for one (court, date). Implemented per court."""
        raise NotImplementedError

    def parse_row(self, row, cells, **ctx):
        """Build a ``(CourtCase, CourtCaseHearing)`` tuple from one table row.

        Return ``None`` to skip the row. Exceptions are caught by
        :meth:`extract_rows` and logged, never aborting the rest of the date.
        """
        raise NotImplementedError

    # --- shared machinery ------------------------------------------------

    async def start(self):
        # The AD->BS conversion of a calendar date is court-independent, so build
        # the (ad_date, nepali_date, date_bs) list ONCE and reuse it across every
        # court instead of re-converting per court (district = 77x fewer calls).
        end_date = self._now_ktm().date() - timedelta(days=SCRAPE_OFFSET_DAYS)
        start_date = end_date - timedelta(days=self.lookback_days())

        today_bs = self._format_bs(
            nepalidate.from_date(datetime.now(KATHMANDU_TZ).date())
        )

        date_list = []
        current = end_date
        while current >= start_date:
            try:
                nd = nepalidate.from_date(current)
                date_list.append((current, nd, self._format_bs(nd)))
            except Exception as e:
                self.logger.error(f"Error converting date {current}: {e}")
            current -= timedelta(days=1)

        for court in self.court_contexts():
            court_key = self.court_key(court)
            scraped_dates = get_scraped_dates(self.session, court_key)
            self.logger.info(
                f"Starting scrape for {court_key}, "
                f"{len(scraped_dates)} dates already processed"
            )

            for ad_date, nepali_date, date_bs in date_list:
                if date_bs in scraped_dates:
                    continue
                for request in self.build_requests_for_date(
                    court, ad_date, nepali_date, date_bs, today_bs
                ):
                    yield request

    def extract_rows(self, rows, **ctx):
        """Map rows -> ``(case, hearing)`` tuples, skipping/​logging bad rows.

        ``ctx`` is forwarded to :meth:`parse_row`; pass ``hearing_date_ad`` in it
        so the per-date BS->AD conversion is done once, not once per row.
        """
        data = []
        for row in rows:
            cells = row.find_all("td")
            try:
                result = self.parse_row(row, cells, **ctx)
            except Exception as e:
                self.logger.error(f"Error parsing row: {e}")
                continue
            if result is not None:
                data.append(result)
        return data

    def save_cases(self, data, court_key, date_bs, note=None):
        """Persist cases + hearings for a date and mark the date scraped.

        Closes the session afterwards so the identity map can't grow unbounded
        across a long crawl (the same session is reused for every date).
        """
        try:
            with self.session.begin():
                for case, hearing in data:
                    self.session.merge(case)
                    self.session.add(hearing)
                mark_date_scraped(self.session, court_key, date_bs, note)
        finally:
            self.session.close()

    # --- per-bench accumulation (high / special) -------------------------

    def record_bench(self, court_key, date_bs, total_benches, new_data, note=None):
        """Accumulate one bench's rows; save the whole date once all benches resolve.

        A bench "resolves" on success *or* via :meth:`bench_errback`, so a single
        failed bench can no longer strand the date (and leak its accumulated rows
        in memory) the way it did before.
        """
        key = (court_key, date_bs)
        self._data_by_date.setdefault(key, []).extend(new_data)
        self._bench_counter[key] = self._bench_counter.get(key, 0) + 1

        if self._bench_counter[key] >= total_benches:
            self.save_cases(self._data_by_date.pop(key, []), court_key, date_bs, note)
            self._bench_counter.pop(key, None)
            self.logger.info(f"Saved all cases for {court_key} on {date_bs}")

    def bench_errback(self, failure):
        """Count a failed bench request as resolved (with no rows) so the date can finalise."""
        meta = failure.request.meta
        court_key = meta.get("court_key")
        date_bs = meta.get("date_bs")
        total_benches = meta.get("total_benches")
        self.logger.error(
            f"Bench request failed for {court_key} on {date_bs}: {failure.value}"
        )
        if court_key and date_bs and total_benches:
            note = meta.get("bench_note")
            self.record_bench(court_key, date_bs, total_benches, [], note)


class BaseCaseEnrichmentSpider(BaseScrapeSpider):
    """Detail-page enrichment spiders.

    Owns the pending-case query, the request loop, failure marking, and the
    unified save: one locked transaction per case (no separate pre-check query,
    no TOCTOU window) that updates core fields + ``extra_data`` and rebuilds its
    ``CaseEntity`` rows (duplicate/junk parties dropped). Re-enrichment fully
    replaces a case's parties, so any externally-resolved ``nes_id`` is not
    carried over.

    Subclasses implement: :meth:`court_filter`, :meth:`build_request`, and the
    court-specific ``parse_case_detail`` that calls :meth:`save_enrichment`.
    """

    custom_settings = {**RETRY_SETTINGS, "CONCURRENT_REQUESTS": 4}

    async def start(self):
        self._init_db()
        cases = self._fetch_pending()
        if not cases:
            self.logger.info(f"No cases to enrich for {self.name}")
            return
        self.logger.info(f"Found {len(cases)} cases to enrich for {self.name}")
        for case_number, court_identifier in cases:
            request = self.build_request(case_number, court_identifier)
            if request is not None:
                yield request

    # --- hooks subclasses implement -------------------------------------

    def court_filter(self):
        """SQLAlchemy predicate selecting this family's ``court_identifier``(s)."""
        raise NotImplementedError

    def needs_enrichment_filter(self):
        """Which rows still need enrichment (override to widen, e.g. backfill)."""
        return or_(CourtCase.status == "pending", CourtCase.status.is_(None))

    def build_request(self, case_number, court_identifier):
        """Build the detail-page request for one case (set ``errback=self.handle_error``)."""
        raise NotImplementedError

    # --- shared machinery ------------------------------------------------

    def _fetch_pending(self):
        with self.session.begin():
            return (
                self.session.query(CourtCase.case_number, CourtCase.court_identifier)
                .filter(and_(self.court_filter(), self.needs_enrichment_filter()))
                .order_by(CourtCase.registration_date_ad.desc().nullslast())
                .all()
            )

    def _get_case(self, case_number, court_identifier, lock=False):
        query = self.session.query(CourtCase).filter(
            and_(
                CourtCase.case_number == case_number,
                CourtCase.court_identifier == court_identifier,
            )
        )
        if lock:
            query = query.with_for_update()
        return query.first()

    def handle_error(self, failure):
        """errback: log and mark the case failed."""
        case_number = failure.request.meta.get("case_number")
        court_identifier = failure.request.meta.get("court_identifier")
        self.logger.error(f"Error enriching case {case_number}: {failure.value}")
        self.mark_failed(case_number, court_identifier)

    def mark_failed(self, case_number, court_identifier):
        try:
            with self.session.begin():
                case = self._get_case(case_number, court_identifier)
                if case:
                    case.status = "failed"
                    case.updated_at = self._now_ktm()
        except Exception:
            self.logger.exception(f"Failed to mark {case_number} as failed")

    def save_enrichment(
        self,
        case_number,
        court_identifier,
        core_fields,
        extra_updates,
        entities,
    ):
        """Apply parsed enrichment in a single locked transaction.

        Returns ``True`` if the row is in the enriched state afterwards (saved now
        or already enriched by a concurrent worker), ``False`` only if the case is
        missing from the DB.
        """
        now = self._now_ktm()
        try:
            with self.session.begin():
                case = self._get_case(case_number, court_identifier, lock=True)
                if not case:
                    self.logger.error(f"Case {case_number} not found for enrichment")
                    return False

                if case.status == "enriched":
                    self.logger.info(f"Case {case_number} already enriched, skipping")
                    return True

                # Reroute legacy fields to extra_data — they are not v2 columns.
                for legacy_key in _LEGACY_EXTRA_FIELDS:
                    if legacy_key in core_fields:
                        extra_updates[legacy_key] = core_fields.pop(legacy_key)

                for key, value in core_fields.items():
                    setattr(case, key, value)

                if case.extra_data is None:
                    case.extra_data = {}
                case.extra_data.update(extra_updates)
                # enriched_at is stashed in extra_data (not a v2 column); updated_at
                # already records the last write.
                case.extra_data["enriched_at"] = now.isoformat()
                flag_modified(case, "extra_data")

                case.status = "enriched"
                case.updated_at = now

                self._replace_entities(case_number, court_identifier, entities, now)
                return True
        finally:
            # Release the session (and its identity map) after each case so memory
            # stays flat across a long enrichment run.
            self.session.close()

    def _replace_entities(self, case_number, court_identifier, entities, now):
        """Rebuild this case's plaintiff/defendant rows (scoped delete + reinsert).

        Parties are passed through :func:`clean_parties` first, so junk/label rows
        are dropped and duplicates collapsed.
        """
        sides = ("plaintiff", "defendant")

        self.session.query(CaseEntity).filter(
            and_(
                CaseEntity.case_number == case_number,
                CaseEntity.court_identifier == court_identifier,
                CaseEntity.side.in_(sides),
            )
        ).delete(synchronize_session=False)

        for side in sides:
            for party in clean_parties(entities.get(f"{side}s", [])):
                self.session.add(
                    CaseEntity(
                        case_number=case_number,
                        court_identifier=court_identifier,
                        side=side,
                        name=party["name"],
                        address=party.get("address"),
                        created_at=now,
                        updated_at=now,
                    )
                )


__all__ = [
    "KATHMANDU_TZ",
    "RETRY_SETTINGS",
    "BaseScrapeSpider",
    "BaseCourtCasesSpider",
    "BaseCaseEnrichmentSpider",
    "CourtCase",
    "CourtCaseHearing",
    "convert_bs_to_ad",
]
