"""
Tests for the fast court-order listing (single recursive list_objects_v2 instead
of per-object is_file() stats). Exercises the S3 branch with a fake boto3 client.
"""

from ngm.index import publish
from ngm.index.build_index import IndexBuilder


class _FakePaginator:
    def __init__(self, keys):
        self.keys = keys

    def paginate(self, Bucket, Prefix):  # noqa: N803
        yield {"Contents": [{"Key": k} for k in self.keys if k.startswith(Prefix)]}


class _FakeListClient:
    """Minimal S3 client supporting Delimiter CommonPrefixes + recursive paginate."""

    def __init__(self, keys):
        self.keys = keys

    def list_objects_v2(self, Bucket, Prefix, Delimiter=None):  # noqa: N803
        cps = set()
        for k in self.keys:
            if k.startswith(Prefix):
                rest = k[len(Prefix) :]
                if "/" in rest:
                    cps.add(Prefix + rest.split("/")[0] + "/")
        return {"CommonPrefixes": [{"Prefix": p} for p in sorted(cps)]}

    def get_paginator(self, name):
        return _FakePaginator(self.keys)


KEYS = [
    "uploads/court-orders/special/081-CR-0081.pdf",
    "uploads/court-orders/special/081-CR-0081.1.docx",
    "uploads/court-orders/supreme/063-CI-0042.1.doc",
    "uploads/court-orders/supreme/063-CI-0043.1.doc",
    "uploads/court-orders/supreme/",  # directory-marker key — must be skipped
]


def _s3_builder(monkeypatch):
    monkeypatch.setattr(
        publish, "_make_client", lambda endpoint_url: _FakeListClient(KEYS)
    )
    return IndexBuilder(
        root_path="s3://ngm",
        base_url="https://ngm-store.jawafdehi.org",
        date_str="2026-06-26",
    )


def test_court_order_types_from_common_prefixes(monkeypatch):
    b = _s3_builder(monkeypatch)
    assert sorted(b._court_order_types()) == ["special", "supreme"]


def test_list_court_type_files_skips_markers(monkeypatch):
    b = _s3_builder(monkeypatch)
    special = b._list_court_type_files("special")
    supreme = b._list_court_type_files("supreme")
    assert {p.name for p in special} == {"081-CR-0081.pdf", "081-CR-0081.1.docx"}
    # the "supreme/" directory-marker key is skipped
    assert {p.name for p in supreme} == {"063-CI-0042.1.doc", "063-CI-0043.1.doc"}


def test_build_court_orders_from_s3_listing(monkeypatch):
    b = _s3_builder(monkeypatch)
    node = b._build_court_orders_node()
    # root court-orders -> [special, supreme] -> year -> case (one manuscript/case)
    types = {c.name for c in node.children}
    assert types == {"special", "supreme"}

    docs = {}

    def walk(n):
        for ms in n.manuscripts:
            docs[ms.document_id] = ms
        for c in n.children:
            walk(c)

    walk(node)
    # special case consolidates its 2 files into one manuscript; supreme has 2 cases
    assert "ngm:court-order:special:081-CR-0081" in docs
    assert "ngm:court-order:supreme:063-CI-0042" in docs
    assert "ngm:court-order:supreme:063-CI-0043" in docs
    special_ms = docs["ngm:court-order:special:081-CR-0081"]
    assert special_ms.url.endswith("081-CR-0081.pdf")  # PDF promoted to RAW
    assert any(link["role"] == "ALTERNATE" for link in special_ms.links)
