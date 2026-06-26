"""
Tests for the pure (network-free) helpers in ngm.index.publish — especially the
delete-safety gate that must never let a sync touch `uploads/` or old snapshots.
"""

import pytest

from ngm.index import publish


class TestContentType:
    @pytest.mark.parametrize(
        "key,expected",
        [
            ("d/ngm/x.html", "text/html; charset=utf-8"),
            ("sitemap.xml", "application/xml"),
            ("index-v2.json", "application/json"),
            ("robots.txt", "text/plain; charset=utf-8"),
            ("uploads/x.pdf", "application/octet-stream"),
        ],
    )
    def test_content_type_for(self, key, expected):
        assert publish.content_type_for(key) == expected


class TestParseS3Uri:
    def test_bucket_only(self):
        assert publish.parse_s3_uri("s3://ngm") == ("ngm", "")

    def test_bucket_and_prefix(self):
        assert publish.parse_s3_uri("s3://ngm/sub") == ("ngm", "sub/")
        assert publish.parse_s3_uri("s3://ngm/sub/") == ("ngm", "sub/")

    def test_rejects_non_s3(self):
        with pytest.raises(ValueError):
            publish.parse_s3_uri("/local/path")


class TestIsSafeToDelete:
    DATE = "2026-06-26"

    @pytest.mark.parametrize(
        "key,expected",
        [
            ("uploads/ciaa/x.pdf", False),  # source data — never
            ("d/ngm/ciaa-press-release/1234.html", True),
            ("indices/2026-06-26/index.json", True),  # current snapshot
            ("indices/2026-06-25/index.json", False),  # older snapshot — never
            ("sitemap.xml", True),
            ("sitemap.court-orders.page-2.xml", True),
            ("index-v2.json", False),  # always overwritten, never deleted
            ("robots.txt", False),
            ("something-else.json", False),
        ],
    )
    def test_is_safe_to_delete(self, key, expected):
        assert publish.is_safe_to_delete(key, self.DATE) is expected


class TestComputeDeleteSet:
    def test_only_managed_orphans_deleted(self):
        date = "2026-06-26"
        local = {"d/ngm/a.html", "sitemap.xml", "index-v2.json"}
        remote = {
            "d/ngm/a.html",  # still present
            "d/ngm/gone.html",  # orphan → delete
            "sitemap.old.xml",  # orphan sitemap → delete
            "uploads/x.pdf",  # source → keep
            "indices/2026-06-25/index.json",  # old snapshot → keep
        }
        assert publish.compute_delete_set(local, remote, date) == {
            "d/ngm/gone.html",
            "sitemap.old.xml",
        }


def test_managed_scan_prefixes():
    assert publish.managed_scan_prefixes("2026-06-26") == [
        "d/",
        "indices/2026-06-26/",
        "sitemap",
    ]


def test_local_keys(tmp_path):
    (tmp_path / "d" / "ngm").mkdir(parents=True)
    (tmp_path / "d" / "ngm" / "a.html").write_text("x", encoding="utf-8")
    (tmp_path / "sitemap.xml").write_text("x", encoding="utf-8")
    assert publish.local_keys(tmp_path) == {"d/ngm/a.html", "sitemap.xml"}


# --- end-to-end publish against an in-memory fake S3 -------------------------


class _FakePaginator:
    def __init__(self, store):
        self.store = store

    def paginate(self, Bucket, Prefix):  # noqa: N803 (boto3 kwarg names)
        yield {"Contents": [{"Key": k} for k in self.store if k.startswith(Prefix)]}


class _FakeS3:
    def __init__(self, existing):
        self.store = dict(existing)
        self.content_types = {}
        self.delete_errors = []  # inject per-object delete failures

    def upload_file(self, filename, bucket, key, ExtraArgs=None):  # noqa: N803
        with open(filename, "rb") as fh:
            self.store[key] = fh.read()
        if ExtraArgs:
            self.content_types[key] = ExtraArgs.get("ContentType")

    def get_paginator(self, name):
        return _FakePaginator(self.store)

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        deleted = []
        for obj in Delete["Objects"]:
            self.store.pop(obj["Key"], None)
            deleted.append({"Key": obj["Key"]})
        return {"Deleted": deleted, "Errors": list(self.delete_errors)}


def test_publish_uploads_and_prunes_only_managed(tmp_path, monkeypatch):
    # Staged tree (what build_index produced locally).
    (tmp_path / "d" / "ngm").mkdir(parents=True)
    (tmp_path / "d" / "ngm" / "a.html").write_text("<html>", encoding="utf-8")
    (tmp_path / "sitemap.xml").write_text("<x/>", encoding="utf-8")
    (tmp_path / "index-v2.json").write_text("{}", encoding="utf-8")
    (tmp_path / "robots.txt").write_text("ok", encoding="utf-8")
    (tmp_path / "indices" / "2026-06-26").mkdir(parents=True)
    (tmp_path / "indices" / "2026-06-26" / "index.json").write_text(
        "{}", encoding="utf-8"
    )

    remote = {
        "d/ngm/a.html": b"old",  # overwritten
        "d/ngm/gone.html": b"orphan",  # → deleted
        "sitemap.old.xml": b"orphan",  # → deleted
        "uploads/x.pdf": b"keep",  # source — must survive
        "indices/2026-06-25/index.json": b"keep",  # old snapshot — must survive
    }
    fake = _FakeS3(remote)
    monkeypatch.setattr(publish, "_make_client", lambda endpoint_url: fake)

    stats = publish.publish(str(tmp_path), "s3://ngm", "2026-06-26")

    # All 5 staged files uploaded (a.html overwritten), with correct Content-Type.
    assert stats["uploaded"] == 5
    assert fake.store["d/ngm/a.html"] == b"<html>"
    assert fake.content_types["d/ngm/a.html"] == "text/html; charset=utf-8"
    assert fake.content_types["sitemap.xml"] == "application/xml"
    # Managed orphans pruned.
    assert stats["deleted"] == 2
    assert "d/ngm/gone.html" not in fake.store
    assert "sitemap.old.xml" not in fake.store
    # Protected areas untouched.
    assert fake.store["uploads/x.pdf"] == b"keep"
    assert fake.store["indices/2026-06-25/index.json"] == b"keep"


def test_publish_raises_on_delete_errors(tmp_path, monkeypatch):
    (tmp_path / "sitemap.xml").write_text("<x/>", encoding="utf-8")
    fake = _FakeS3({"d/ngm/gone.html": b"orphan"})  # orphan → goes to delete
    fake.delete_errors = [{"Key": "x", "Code": "AccessDenied", "Message": "no"}]
    monkeypatch.setattr(publish, "_make_client", lambda endpoint_url: fake)
    with pytest.raises(RuntimeError, match="delete_objects failed"):
        publish.publish(str(tmp_path), "s3://ngm", "2026-06-26")


def test_make_client_pool_matches_workers():
    # The boto3 connection pool must be sized to the upload worker count so the
    # parallel upload doesn't thrash connections (the default pool is 10).
    client = publish._make_client(None)
    assert client.meta.config.max_pool_connections == publish._UPLOAD_WORKERS
