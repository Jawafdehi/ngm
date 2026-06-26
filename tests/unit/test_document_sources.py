"""Unit + parity tests for ngm.utils.document_sources.

The helper builds the court_cases.document_sources value. A parity test pins it
to the index's court-order link logic (ngm/index/build_index.py) so the two
code paths that emit DocumentSource links cannot drift.
"""

from ngm.utils.document_sources import (
    DEFAULT_STORE_BASE_URL,
    file_links,
    get_store_base_url,
    order_document_sources,
    primary_url,
)

BASE = "https://store.example"


def test_file_links_pdf_is_raw_rest_alternate():
    urls = [
        "https://x/a.docx",
        "https://x/b.pdf",
        "https://x/c.doc",
    ]
    links = file_links(urls)
    assert links[0] == {"link": "https://x/b.pdf", "role": "RAW"}  # pdf floated first
    assert [lk["role"] for lk in links[1:]] == ["ALTERNATE", "ALTERNATE"]
    assert primary_url(links) == "https://x/b.pdf"


def test_file_links_single_non_pdf_is_raw():
    links = file_links(["https://x/only.docx"])
    assert links == [{"link": "https://x/only.docx", "role": "RAW"}]


def test_file_links_empty():
    assert file_links([]) == []
    assert file_links([None, ""]) == []
    assert primary_url([]) == ""


def test_order_document_sources_shape():
    ds = order_document_sources(
        "supreme",
        "082-WO-0123",
        [
            "court-orders/supreme/082-WO-0123.1.pdf",
            "court-orders/supreme/082-WO-0123.2.docx",
        ],
        base_url=BASE,
    )
    assert len(ds) == 1
    src = ds[0]
    assert src["document_id"] == "ngm:court-order:supreme:082-WO-0123"
    assert src["source_type"] == "COURT_ORDER"
    assert src["url"] == f"{BASE}/court-orders/supreme/082-WO-0123.1.pdf"
    assert src["links"] == [
        {"link": f"{BASE}/court-orders/supreme/082-WO-0123.1.pdf", "role": "RAW"},
        {
            "link": f"{BASE}/court-orders/supreme/082-WO-0123.2.docx",
            "role": "ALTERNATE",
        },
    ]


def test_order_document_sources_strips_leading_slash():
    ds = order_document_sources(
        "special",
        "082-CR-0051",
        ["/court-orders/special/082-CR-0051.1.doc"],
        base_url=BASE,
    )
    assert ds[0]["links"][0]["link"] == f"{BASE}/court-orders/special/082-CR-0051.1.doc"


def test_order_document_sources_empty_paths():
    assert order_document_sources("supreme", "082-WO-0001", []) == []
    assert order_document_sources("supreme", "082-WO-0001", [None, ""]) == []
    assert order_document_sources("supreme", "082-WO-0001", None) == []  # no TypeError


def test_get_store_base_url_default_and_override(monkeypatch):
    monkeypatch.delenv("NGM_STORE_BASE_URL", raising=False)
    assert get_store_base_url() == DEFAULT_STORE_BASE_URL
    assert get_store_base_url("https://x/") == "https://x"
    monkeypatch.setenv("NGM_STORE_BASE_URL", "https://env-store/")
    assert get_store_base_url() == "https://env-store"


def test_parity_with_index_file_links():
    """util.file_links must match IndexBuilder._file_links byte-for-byte."""
    from ngm.index.build_index import IndexBuilder

    for urls in (
        ["https://x/a.docx", "https://x/b.pdf"],
        ["https://x/only.pdf"],
        ["https://x/1.doc", "https://x/2.doc", "https://x/3.pdf"],
        [],
    ):
        assert file_links(urls) == IndexBuilder._file_links(urls)


def test_parity_with_index_base_url(monkeypatch):
    from ngm.index.build_index import get_base_url

    monkeypatch.delenv("NGM_STORE_BASE_URL", raising=False)
    assert get_store_base_url() == get_base_url()
    monkeypatch.setenv("NGM_STORE_BASE_URL", "https://other-store/")
    assert get_store_base_url() == get_base_url()
