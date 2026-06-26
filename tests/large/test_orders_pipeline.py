"""SupremeCourtOrdersPipeline failure classification (transient vs permanent).

Pins the fix for the 13 transient FileException downloads that were wrongly
persisted as permanent orders_failed and so never retried.
"""

import logging
from types import SimpleNamespace

from ngm.database.models import CourtCase
from ngm.ngscrape.pipelines import SupremeCourtOrdersPipeline


class _FakeSpider:
    def __init__(self, session):
        self.session = session
        self.logger = logging.getLogger("test-orders")
        self.successful_cases = 0
        self.failed_cases = 0


def _pipeline(session):
    p = SupremeCourtOrdersPipeline.__new__(SupremeCourtOrdersPipeline)
    p.session = session
    return p


def _seed(session, case_number):
    with session.begin():
        session.add(
            CourtCase(
                case_number=case_number,
                court_identifier="special",
                status="enriched",
            )
        )


def _extra(session, case_number):
    with session.begin():
        case = (
            session.query(CourtCase)
            .filter_by(case_number=case_number, court_identifier="special")
            .first()
        )
        return dict(case.extra_data or {})


def test_transient_fileexception_is_not_permanent(session):
    _seed(session, "082-CR-0270")
    p = _pipeline(session)
    info = SimpleNamespace(spider=_FakeSpider(session))
    item = {
        "case_number": "082-CR-0270",
        "court_identifier": "special",
        "file_urls": ["https://x/a.docx"],
    }
    results = [
        (False, "[Failure: scrapy.pipelines.files.FileException _process_request]")
    ]

    p.item_completed(results, item, info)

    extra = _extra(session, "082-CR-0270")
    assert "orders_failed" not in extra  # stays re-crawlable
    assert extra["orders_transient_retries"] == 1
    assert "orders_transient_error" in extra


def test_no_docs_old_case_is_permanent(session):
    _seed(session, "082-CR-0271")
    p = _pipeline(session)
    info = SimpleNamespace(spider=_FakeSpider(session))
    item = {
        "case_number": "082-CR-0271",
        "court_identifier": "special",
        "file_urls": [],
        "error": "no_docs_old_case",
    }

    p.item_completed([], item, info)

    extra = _extra(session, "082-CR-0271")
    assert extra["orders_failed"] is True
    assert extra["orders_error"] == "no_docs_old_case"


def test_success_writes_court_orders_and_document_sources(session):
    """A successful download dual-writes: extra_data['court_orders'] (scrape state)
    AND the structured document_sources column (presentation surface)."""
    _seed(session, "082-CR-0300")
    p = _pipeline(session)
    info = SimpleNamespace(spider=_FakeSpider(session))
    item = {
        "case_number": "082-CR-0300",
        "court_identifier": "special",
        "file_urls": ["https://src/a.pdf", "https://src/b.docx"],
    }
    results = [
        (True, {"path": "court-orders/special/082-CR-0300.1.pdf"}),
        (True, {"path": "court-orders/special/082-CR-0300.2.docx"}),
    ]

    p.item_completed(results, item, info)

    with session.begin():
        case = (
            session.query(CourtCase)
            .filter_by(case_number="082-CR-0300", court_identifier="special")
            .first()
        )
        extra = dict(case.extra_data or {})
        ds = case.document_sources

    # scrape-state marker preserved (selection query still depends on it)
    assert extra["court_orders"] == [
        "court-orders/special/082-CR-0300.1.pdf",
        "court-orders/special/082-CR-0300.2.docx",
    ]
    assert "court_orders_scraped_at" in extra
    assert "orders_failed" not in extra

    # structured DocumentSource column — one source per case
    assert isinstance(ds, list) and len(ds) == 1
    src = ds[0]
    assert src["document_id"] == "ngm:court-order:special:082-CR-0300"
    assert src["source_type"] == "COURT_ORDER"
    assert src["links"][0]["role"] == "RAW"  # pdf first
    assert src["links"][0]["link"].endswith("082-CR-0300.1.pdf")
    assert src["links"][1]["role"] == "ALTERNATE"
    assert src["url"] == src["links"][0]["link"]
    assert info.spider.successful_cases == 1


def test_transient_escalates_after_max_retries(session):
    _seed(session, "082-CR-0272")
    p = _pipeline(session)
    info = SimpleNamespace(spider=_FakeSpider(session))
    item = {
        "case_number": "082-CR-0272",
        "court_identifier": "special",
        "file_urls": ["https://x/a.docx"],
    }
    results = [(False, "FileException")]

    for _ in range(SupremeCourtOrdersPipeline.MAX_TRANSIENT_RETRIES):
        p.item_completed(results, item, info)

    extra = _extra(session, "082-CR-0272")
    assert (
        extra["orders_transient_retries"]
        == SupremeCourtOrdersPipeline.MAX_TRANSIENT_RETRIES
    )
    assert extra["orders_failed"] is True  # escalated once retries are exhausted
