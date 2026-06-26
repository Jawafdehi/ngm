"""
Tests for the read-root / write-root split: the builder reads scraper uploads
from root_path but writes the derived tree (index + SEO) to output_root (the
local staging dir), leaving the read store untouched.
"""

import json

from ngm.index.build_index import IndexBuilder


def _seed_uploads(root):
    up = root / "uploads"
    (up / "supreme-court" / "kanun-patrika").mkdir(parents=True)
    (up / "supreme-court" / "kanun-patrika" / "2080-Bhadra.pdf").write_bytes(b"%PDF x")


def test_writes_go_to_output_root_not_read_root(tmp_path):
    read_root = tmp_path / "store"  # where scraper uploads live
    staging = tmp_path / "staging"  # local build output
    read_root.mkdir()
    staging.mkdir()
    _seed_uploads(read_root)

    builder = IndexBuilder(
        root_path=str(read_root),
        base_url="https://ngm-store.example.org",
        date_str="2026-06-26",
        output_path=str(staging),
    )
    root = builder.build_tree()
    root.validate()
    builder.write_index_files(root)

    # Derived tree written under staging.
    assert (staging / "index-v2.json").exists()
    assert (staging / "indices" / "2026-06-26" / "index.json").exists()
    assert (staging / "sitemap.xml").exists()
    assert (staging / "robots.txt").exists()
    assert (staging / "d" / "ngm" / "kanun-patrika" / "2080-bhadra.html").exists()

    # Read store still only has uploads — no derived files leaked into it.
    assert (read_root / "uploads").exists()
    assert not (read_root / "index-v2.json").exists()
    assert not (read_root / "indices").exists()
    assert not (read_root / "d").exists()
    assert not (read_root / "sitemap.xml").exists()

    # Sanity: the index actually indexed the seeded document.
    content = json.loads((staging / "index-v2.json").read_text(encoding="utf-8"))
    assert content["name"] == "root"


def test_default_output_root_equals_read_root(tmp_path):
    _seed_uploads(tmp_path)
    builder = IndexBuilder(
        root_path=str(tmp_path),
        base_url="https://ngm-store.example.org",
        date_str="2026-06-26",
    )
    builder.write_index_files(builder.build_tree())
    # No output_path → writes land in the read root (legacy/local behavior).
    assert (tmp_path / "index-v2.json").exists()
    assert (tmp_path / "sitemap.xml").exists()
