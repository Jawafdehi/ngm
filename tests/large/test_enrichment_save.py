"""End-to-end test of the shared enrichment save against a real (SQLite) DB.

Exercises the fixes that live in BaseCaseEnrichmentSpider.save_enrichment:
entity dedup + junk/placeholder stripping (I10/I11), the typed columns
persisting (case_subject/hearing_count/verdict_type), enriched_at stashed in
extra_data (a v2 non-column), and single-transaction idempotency (I3/C3).
"""

from datetime import date, datetime

from ngm.database.models import CaseEntity, CourtCase, CourtCaseHearing
from ngm.ngscrape.base_spiders import BaseCaseEnrichmentSpider, BaseCourtCasesSpider


class _DummyEnrichSpider(BaseCaseEnrichmentSpider):
    name = "dummy_enrich"

    def court_filter(self):
        return CourtCase.court_identifier == "kathmandudc"

    def build_request(self, case_number, court_identifier):
        return None


def _make(session):
    sp = _DummyEnrichSpider.__new__(_DummyEnrichSpider)
    sp.session = session
    return sp


def test_save_enrichment_roundtrip(session):
    with session.begin():
        session.add(
            CourtCase(
                case_number="081-CR-0001",
                court_identifier="kathmandudc",
                status="pending",
            )
        )

    sp = _make(session)
    entities = {
        "plaintiffs": [
            {"name": "  रामबहादुर  ", "address": None},  # padded
            {"name": "रामबहादुर", "address": None},  # exact duplicate
            {"name": "वादी", "address": None},  # header label
        ],
        "defendants": [
            {"name": "श्यामबहादुर", "address": "काठमाडौं"},
            {"name": "-", "address": None},  # placeholder
        ],
    }
    core = {
        "case_type": "भ्रष्टाचार",
        "case_subject": "घुस लिएको",
        "hearing_count": 3,  # spiders coerce to int (Integer column in v2)
        "verdict_type": "फैसला",
    }
    extra = {"enrichment_hearings": [{"date": "2081-01-01"}]}

    assert (
        sp.save_enrichment("081-CR-0001", "kathmandudc", core, extra, entities) is True
    )

    with session.begin():
        case = (
            session.query(CourtCase)
            .filter_by(case_number="081-CR-0001", court_identifier="kathmandudc")
            .first()
        )
        assert case.status == "enriched"
        assert case.extra_data["enriched_at"] is not None  # stashed in extra_data (v2)
        assert case.case_subject == "घुस लिएको"
        assert case.hearing_count == 3
        assert case.verdict_type == "फैसला"
        assert case.extra_data["enrichment_hearings"] == [{"date": "2081-01-01"}]

        ents = session.query(CaseEntity).filter_by(case_number="081-CR-0001").all()
        plaintiffs = sorted(e.name for e in ents if e.side == "plaintiff")
        defendants = [(e.name, e.address) for e in ents if e.side == "defendant"]
        # padded+dup collapsed to one; 'वादी' label dropped
        assert plaintiffs == ["रामबहादुर"]
        # '-' placeholder dropped; real address preserved
        assert defendants == [("श्यामबहादुर", "काठमाडौं")]


def test_save_enrichment_reroutes_legacy_fields_to_extra_data(session):
    # The 7 low-value legacy fields are not v2 columns; save_enrichment stashes
    # any that arrive in core_fields into extra_data instead of a dropped column.
    with session.begin():
        session.add(
            CourtCase(
                case_number="081-CR-0003",
                court_identifier="kathmandudc",
                status="pending",
            )
        )
    sp = _make(session)
    core = {"division": "रिट १", "category": "फाँट क", "case_subject": "x"}
    sp.save_enrichment(
        "081-CR-0003", "kathmandudc", core, {}, {"plaintiffs": [], "defendants": []}
    )
    with session.begin():
        case = (
            session.query(CourtCase)
            .filter_by(case_number="081-CR-0003", court_identifier="kathmandudc")
            .first()
        )
        assert case.extra_data["division"] == "रिट १"
        assert case.extra_data["category"] == "फाँट क"
        assert case.case_subject == "x"  # real typed columns still persist
    assert not hasattr(CourtCase, "division")  # column truly removed from the model


def test_save_enrichment_idempotent_when_already_enriched(session):
    with session.begin():
        session.add(
            CourtCase(
                case_number="081-CR-0002",
                court_identifier="kathmandudc",
                status="enriched",
                case_type="ORIGINAL",
            )
        )

    sp = _make(session)
    saved = sp.save_enrichment(
        "081-CR-0002",
        "kathmandudc",
        {"case_type": "OVERWRITE"},
        {},
        {"plaintiffs": [], "defendants": []},
    )
    assert saved is True  # already enriched -> skipped, but reported healthy

    with session.begin():
        case = (
            session.query(CourtCase)
            .filter_by(case_number="081-CR-0002", court_identifier="kathmandudc")
            .first()
        )
        assert case.case_type == "ORIGINAL"  # not clobbered


class _DummyCourtCasesSpider(BaseCourtCasesSpider):
    name = "dummy_court_cases"

    def parse_row(self, row, cells, **ctx):
        return None


def test_save_cases_relist_preserves_enriched_extra_data(session):
    # Regression: re-listing an already-enriched case on a new hearing date must
    # NOT clobber its extra_data. merge() replaces the whole JSONB column, so
    # save_cases unsets the transient's extra_data and unions the listing stash.
    with session.begin():
        session.add(
            CourtCase(
                case_number="081-CR-7",
                court_identifier="kathmandudc",
                status="enriched",
                extra_data={
                    "division": "रिट १",
                    "enrichment_hearings": [{"d": "2081"}],
                    "enriched_at": "2081-01-01",
                },
            )
        )

    sp = _DummyCourtCasesSpider.__new__(_DummyCourtCasesSpider)
    sp.session = session
    fresh = CourtCase(
        case_number="081-CR-7",
        court_identifier="kathmandudc",
        extra_data={"division": "रिट १", "section": "मुद्दा"},  # listing stash
    )
    hearing = CourtCaseHearing(
        case_number="081-CR-7",
        court_identifier="kathmandudc",
        hearing_date_bs="2081-05-05",
        hearing_date_ad=date(2024, 8, 20),
        scraped_at=datetime(2024, 8, 20),
    )
    sp.save_cases([(fresh, hearing)], "kathmandudc", "2081-05-05")

    with session.begin():
        got = session.get(CourtCase, ("081-CR-7", "kathmandudc"))
        assert got.extra_data["enrichment_hearings"] == [{"d": "2081"}]  # preserved
        assert got.extra_data["enriched_at"] == "2081-01-01"  # preserved
        assert got.extra_data["section"] == "मुद्दा"  # listing stash merged in
        assert got.status == "enriched"


def test_save_enrichment_missing_case_returns_false(session):
    sp = _make(session)
    saved = sp.save_enrichment(
        "999-XX-9999", "kathmandudc", {}, {}, {"plaintiffs": [], "defendants": []}
    )
    assert saved is False
