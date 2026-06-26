"""Behavioural tests for the shared listing-spider machinery in base_spiders.

No HTML fixtures or DB — these pin the bug fixes that live in the base class:
the per-row extraction guard (I4) and bench finalisation via errback (I5).
"""

from types import SimpleNamespace

from bs4 import BeautifulSoup

from ngm.ngscrape.base_spiders import BaseCourtCasesSpider


class _DummyCourtSpider(BaseCourtCasesSpider):
    name = "dummy_court"

    def parse_row(self, row, cells, **ctx):
        text = cells[0].get_text() if cells else ""
        if text == "boom":
            raise ValueError("bad row")
        return (text, "hearing")


def _make():
    sp = _DummyCourtSpider.__new__(_DummyCourtSpider)
    sp._bench_counter = {}
    sp._data_by_date = {}
    sp.saved = []
    sp.save_cases = lambda data, ck, db, note=None: sp.saved.append(
        (ck, db, note, list(data))
    )
    return sp


def _rows(*texts):
    html = "<table>" + "".join(f"<tr><td>{t}</td></tr>" for t in texts) + "</table>"
    return BeautifulSoup(html, "html.parser").find_all("tr")


def test_extract_rows_skips_a_raising_row():
    # One malformed row must not sink the rest of the day (I4).
    sp = _make()
    out = sp.extract_rows(_rows("1", "boom", "2"))
    assert out == [("1", "hearing"), ("2", "hearing")]


def test_record_bench_finalizes_only_after_all_benches():
    sp = _make()
    sp.record_bench("kathmandudc", "2081-01-01", 3, [("a", 1)], "3 benches")
    sp.record_bench("kathmandudc", "2081-01-01", 3, [("b", 2)], "3 benches")
    assert sp.saved == []  # still waiting on the 3rd bench
    sp.record_bench("kathmandudc", "2081-01-01", 3, [], "3 benches")
    assert len(sp.saved) == 1
    court_key, date_bs, note, data = sp.saved[0]
    assert (court_key, date_bs, note) == ("kathmandudc", "2081-01-01", "3 benches")
    assert len(data) == 2


def test_bench_errback_lets_the_date_finalize():
    # A failed bench request counts as resolved, so the date still saves (I5).
    sp = _make()
    sp.record_bench("patanhc", "2081-02-02", 2, [("a", 1)], "2 benches")
    assert sp.saved == []
    failure = SimpleNamespace(
        request=SimpleNamespace(
            meta={
                "court_key": "patanhc",
                "date_bs": "2081-02-02",
                "total_benches": 2,
                "bench_note": "2 benches",
            }
        ),
        value="connection reset",
    )
    sp.bench_errback(failure)
    assert len(sp.saved) == 1
