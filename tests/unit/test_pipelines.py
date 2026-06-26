"""Unit tests for pipeline file naming (no FilesPipeline machinery needed)."""

from types import SimpleNamespace

from ngm.ngscrape.pipelines import CiaaPressReleasesPipeline, SupremeCourtOrdersPipeline


def _req(url):
    return SimpleNamespace(url=url)


def test_orders_file_path_naming():
    p = SupremeCourtOrdersPipeline.__new__(SupremeCourtOrdersPipeline)
    url = "https://supremecourt.gov.np/x/order.docx"
    item = {
        "court_identifier": "special",
        "case_number": "082-OA-0503",
        "file_urls": [url],
    }
    assert (
        p.file_path(_req(url), item=item) == "court-orders/special/082-OA-0503.1.docx"
    )


def test_orders_file_path_slash_in_case_number_is_safe():
    p = SupremeCourtOrdersPipeline.__new__(SupremeCourtOrdersPipeline)
    url = "https://x/a.doc"
    item = {
        "court_identifier": "special",
        "case_number": "082/OA/0503",
        "file_urls": [url],
    }
    # Slash replaced with dash so the case number can't escape the directory.
    assert p.file_path(_req(url), item=item) == "court-orders/special/082-OA-0503.1.doc"


def test_orders_file_path_sequential_numbering():
    p = SupremeCourtOrdersPipeline.__new__(SupremeCourtOrdersPipeline)
    urls = ["https://x/a.docx", "https://x/b.docx"]
    item = {
        "court_identifier": "supreme",
        "case_number": "081-WO-0001",
        "file_urls": urls,
    }
    assert p.file_path(_req(urls[1]), item=item).endswith("081-WO-0001.2.docx")


def test_safe_filename_strips_unsafe_chars():
    p = CiaaPressReleasesPipeline.__new__(CiaaPressReleasesPipeline)
    assert p._safe_filename('a/b:c*?"<>|') == "abc"


def test_safe_filename_byte_truncation_keeps_valid_utf8():
    p = CiaaPressReleasesPipeline.__new__(CiaaPressReleasesPipeline)
    out = p._safe_filename("अनियमितता" * 100, max_bytes=9)
    assert 1 <= len(out.encode("utf-8")) <= 9
    # never split a multibyte char mid-sequence
    out.encode("utf-8").decode("utf-8")
