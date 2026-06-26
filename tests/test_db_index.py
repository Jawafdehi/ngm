"""
Tests for ngm.index.db_index.collect_rows — the pure tree → row-dict mapping
that feeds the Postgres document_sources mirror. (The upsert/delete sweep needs
a real Postgres and is verified manually.)
"""

from ngm.index.db_index import collect_rows
from ngm.index.models import IndexNode, Manuscript

BASE = "https://ngm-store.example.org"


def _tree():
    pr = IndexNode(
        name="ciaa-press-releases",
        path="/ciaa-press-releases",
        manuscripts=[
            Manuscript(
                url="https://s/charge.pdf",
                file_name="charge.pdf",
                metadata={
                    "title": "मुद्दा",
                    "full_text": "विवरण",
                    "publication_date": "2082-12-08",
                },
                links=[
                    {"link": "https://s/charge.pdf", "role": "RAW"},
                    {"link": "https://ciaa.gov.np/p/1234", "role": "SOURCE_PAGE"},
                ],
                document_id="ngm:ciaa-press-release:1234",
                source_type="CIAA_PRESS_RELEASE",
            ),
            # No document_id → must be skipped (not a real indexed document).
            Manuscript(url="https://s/x.pdf", file_name="x.pdf"),
        ],
    )
    leaf = IndexNode(
        name="081-CR-0081",
        path="/court-orders/special/081/081-CR-0081",
        manuscripts=[
            Manuscript(
                url="https://s/o.pdf",
                file_name="o.pdf",
                links=[{"link": "https://s/o.pdf", "role": "RAW"}],
                document_id="ngm:court-order:special:081-CR-0081",
                source_type="COURT_ORDER",
            )
        ],
    )
    year = IndexNode(name="081", path="/court-orders/special/081", children=[leaf])
    special = IndexNode(name="special", path="/court-orders/special", children=[year])
    court = IndexNode(name="court-orders", path="/court-orders", children=[special])
    return IndexNode(name="root", path="/", children=[pr, court])


def test_collect_rows_one_per_logical_doc_skips_no_id():
    rows = collect_rows(_tree(), BASE, "2026-06-26")
    assert len(rows) == 2  # the no-document_id manuscript is dropped
    assert {r["document_id"] for r in rows} == {
        "ngm:ciaa-press-release:1234",
        "ngm:court-order:special:081-CR-0081",
    }


def test_press_release_row_fields():
    rows = {r["document_id"]: r for r in collect_rows(_tree(), BASE, "2026-06-26")}
    pr = rows["ngm:ciaa-press-release:1234"]
    assert pr["dataset"] == "ciaa-press-releases"
    assert pr["source_type"] == "CIAA_PRESS_RELEASE"
    assert pr["title"] == "मुद्दा"
    assert pr["publication_date_bs"] == "2082-12-08"
    assert pr["primary_url"] == "https://s/charge.pdf"
    assert (
        pr["html_url"]
        == "https://ngm-store.example.org/d/ngm/ciaa-press-release/1234.html"
    )
    assert {link["role"] for link in pr["links"]} == {"RAW", "SOURCE_PAGE"}
    assert pr["index_path"] == "/ciaa-press-releases"
    assert pr["last_seen_build"] == "2026-06-26"


def test_court_order_row_dataset_is_top_level():
    rows = {r["document_id"]: r for r in collect_rows(_tree(), BASE, "2026-06-26")}
    co = rows["ngm:court-order:special:081-CR-0081"]
    # dataset is the top-level tree node, not the nested court/year.
    assert co["dataset"] == "court-orders"
    assert (
        co["html_url"]
        == "https://ngm-store.example.org/d/ngm/court-order/special/081-CR-0081.html"
    )
    assert co["index_path"] == "/court-orders/special/081/081-CR-0081"
