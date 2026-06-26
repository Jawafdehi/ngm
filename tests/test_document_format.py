"""
Tests for the DocumentSource-compatible manuscript format.

Covers:
- Manuscript carries links / document_id / source_type and round-trips them,
  while staying backward-compatible (optional fields omitted when empty).
- IndexBuilder link helpers (RAW/ALTERNATE ordering, SOURCE_PAGE, html paths).
- Per-dataset builders emit ONE manuscript per logical document with a valid
  roled link list and a stable document_id.
- Every generated link list satisfies the same rules jawafdehi-api's
  ``validate_url_list`` enforces, so a future 1:1 DocumentSource import is clean.
"""

import json
import re

import pytest

from ngm.index.build_index import IndexBuilder, _slugify, _slug_with_hash
from ngm.index.models import Manuscript, SourceLinkRole, SourceType


VALID_ROLES = {r.value for r in SourceLinkRole}


def assert_links_documentsource_compatible(links):
    """Mirror jawafdehi-api cases.models.validate_url_list rules."""
    assert isinstance(links, list)
    for item in links:
        assert isinstance(item, dict)
        link = item.get("link")
        assert isinstance(link, str) and link.strip(), f"bad link: {item!r}"
        assert item.get("role") in VALID_ROLES, f"bad role: {item!r}"


def make_builder(tmp_path):
    return IndexBuilder(
        root_path=str(tmp_path),
        base_url="https://ngm-store.example.org",
        date_str="2026-06-26",
    )


# --- fixture store -----------------------------------------------------------


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _touch(path, content=b"%PDF-1.4 fake"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture
def store(tmp_path):
    """Populate a realistic uploads/ tree for all five datasets."""
    up = tmp_path / "uploads"

    # kanun-patrika
    _touch(up / "supreme-court" / "kanun-patrika" / "2080-Bhadra.pdf")

    # ciaa annual reports (pdf + metadata sidecar)
    _touch(up / "ciaa" / "annual-reports" / "pdf" / "report-2080.pdf")
    _write_json(
        up / "ciaa" / "annual-reports" / "metadata" / "report-2080.json",
        {"title": "CIAA Annual Report 2080", "serial_number": "31", "date": "2080"},
    )

    # ciaa press release with TWO attachments (pdf + doc) and a source page
    _write_json(
        up / "ciaa" / "press-releases" / "metadata" / "1234.json",
        {
            "press_id": 1234,
            "title": "भ्रष्टाचार मुद्दा दायर",
            "full_text": "अख्तियारले मुद्दा दायर गर्‍यो।",
            "publication_date": "2082-12-08",
            "source_url": "https://ciaa.gov.np/pressrelease/1234",
            "file_names": ["charge.doc", "charge.pdf"],
        },
    )
    _touch(up / "ciaa" / "press-releases" / "files" / "charge.pdf")
    _touch(up / "ciaa" / "press-releases" / "files" / "charge.doc")

    # ppmo blacklist (no file, source page only)
    _write_json(
        up / "ppmo" / "blacklist" / "metadata" / "2082-01-01-acme.json",
        {"firm_name": "Acme Nirman", "source_url": "https://ppmo.gov.np/blacklist/1"},
    )

    # court orders: one case, two files (pdf + docx version)
    _touch(up / "court-orders" / "special" / "081-CR-0081.pdf")
    _touch(up / "court-orders" / "special" / "081-CR-0081.1.docx")

    return tmp_path


# --- Manuscript model --------------------------------------------------------


class TestManuscriptFormat:
    def test_optional_fields_omitted_when_empty(self):
        ms = Manuscript(url="https://x/a.pdf", file_name="a.pdf")
        assert ms.to_dict() == {
            "url": "https://x/a.pdf",
            "file_name": "a.pdf",
            "metadata": {},
        }

    def test_links_serialized_when_present(self):
        links = [{"link": "https://x/a.pdf", "role": "RAW"}]
        ms = Manuscript(
            url="https://x/a.pdf",
            file_name="a.pdf",
            links=links,
            document_id="ngm:kanun-patrika:a",
            source_type=SourceType.MISC.value,
        )
        d = ms.to_dict()
        assert d["links"] == links
        assert d["document_id"] == "ngm:kanun-patrika:a"
        assert d["source_type"] == "MISC"

    def test_round_trip_preserves_new_fields(self):
        ms = Manuscript(
            url="https://x/a.pdf",
            file_name="a.pdf",
            links=[{"link": "https://x/a.pdf", "role": "RAW"}],
            document_id="ngm:court-order:special:081-CR-0081",
            source_type="COURT_ORDER",
        )
        back = Manuscript.from_dict(ms.to_dict())
        assert back.links == ms.links
        assert back.document_id == ms.document_id
        assert back.source_type == ms.source_type

    def test_from_dict_defaults_for_legacy(self):
        ms = Manuscript.from_dict({"url": "https://x/a.pdf", "file_name": "a.pdf"})
        assert ms.links == []
        assert ms.document_id == ""
        assert ms.source_type == ""


# --- link helpers ------------------------------------------------------------


class TestLinkHelpers:
    def test_file_links_pdf_is_raw_rest_alternate(self):
        links = IndexBuilder._file_links(
            ["https://x/a.doc", "https://x/b.pdf", "https://x/c.docx"]
        )
        assert links[0] == {"link": "https://x/b.pdf", "role": "RAW"}
        assert all(item["role"] == "ALTERNATE" for item in links[1:])
        assert {item["link"] for item in links[1:]} == {
            "https://x/a.doc",
            "https://x/c.docx",
        }

    def test_file_links_empty(self):
        assert IndexBuilder._file_links([]) == []

    def test_append_source_page(self):
        links = []
        IndexBuilder._append_source_page(links, "https://gov/page")
        IndexBuilder._append_source_page(links, "  ")
        assert links == [{"link": "https://gov/page", "role": "SOURCE_PAGE"}]

    def test_document_html_relpath_is_lossless(self):
        rel = IndexBuilder._document_html_relpath("ngm:ciaa-press-release:1234")
        assert rel == "d/ngm/ciaa-press-release/1234.html"

    def test_slugify_keeps_devanagari(self):
        assert _slugify("2080 Bhadra Issue") == "2080-bhadra-issue"
        assert _slugify("कानून") == "कानून"
        assert _slugify("") == "doc"

    def test_slug_with_hash_is_collision_free(self):
        # Distinct inputs that slugify identically must get distinct ids.
        assert _slugify("Report (2080)") == _slugify("Report 2080") == "report-2080"
        a = _slug_with_hash("Report (2080)")
        b = _slug_with_hash("Report 2080")
        assert a != b
        assert a.startswith("report-2080-")
        assert _slug_with_hash("Report 2080") == b  # deterministic


# --- per-dataset builders ----------------------------------------------------


class TestBuildersProduceLogicalDocuments:
    def test_kanun_patrika(self, store):
        node = make_builder(store)._build_kanun_patrika_node()
        assert len(node.manuscripts) == 1
        ms = node.manuscripts[0]
        assert ms.document_id.startswith("ngm:kanun-patrika:2080-bhadra-")
        assert re.fullmatch(r"[0-9a-f]{8}", ms.document_id.rsplit("-", 1)[-1])
        assert ms.source_type == SourceType.MISC.value
        assert ms.links == [
            {"link": ms.url, "role": "RAW"},
        ]
        assert_links_documentsource_compatible(ms.links)

    def test_annual_report(self, store):
        node = make_builder(store)._build_ciaa_annual_reports_node()
        ms = node.manuscripts[0]
        assert ms.document_id.startswith("ngm:ciaa-annual-report:report-2080-")
        assert ms.links[0]["role"] == "RAW"
        assert_links_documentsource_compatible(ms.links)

    def test_press_release_consolidates_attachments(self, store):
        node = make_builder(store)._build_ciaa_press_releases_node()
        # Two attachments => still ONE manuscript for the release.
        assert len(node.manuscripts) == 1
        ms = node.manuscripts[0]
        assert ms.document_id == "ngm:ciaa-press-release:1234"
        assert ms.source_type == SourceType.CIAA_PRESS_RELEASE.value
        roles = [link["role"] for link in ms.links]
        assert roles.count("RAW") == 1
        assert roles.count("ALTERNATE") == 1
        assert "SOURCE_PAGE" in roles
        # PDF wins RAW over the .doc attachment.
        raw = next(link for link in ms.links if link["role"] == "RAW")
        assert raw["link"].endswith("charge.pdf")
        assert_links_documentsource_compatible(ms.links)

    def test_press_release_press_id_falls_back_to_filename(self, tmp_path):
        # Metadata files are named {press_id}.json; if the body omits press_id,
        # the id must fall back to the stem (never "...:None").
        builder = make_builder(tmp_path)
        files_dir = tmp_path / "files"
        files_dir.mkdir()
        meta_path = tmp_path / "5678.json"
        meta_path.write_text(
            json.dumps(
                {
                    "title": "x",
                    "file_names": ["a.pdf"],
                    "source_url": "https://ciaa.gov.np/p/5678",
                }
            ),
            encoding="utf-8",
        )
        ms = builder._process_press_release_metadata(meta_path, files_dir)
        assert ms.document_id == "ngm:ciaa-press-release:5678"

    def test_ppmo_blacklist_source_page_only(self, store):
        node = make_builder(store)._build_ppmo_blacklist_node()
        ms = node.manuscripts[0]
        assert ms.document_id.startswith("ngm:ppmo-blacklist:")
        assert ms.links == [
            {"link": "https://ppmo.gov.np/blacklist/1", "role": "SOURCE_PAGE"}
        ]
        assert_links_documentsource_compatible(ms.links)

    def test_court_order_collapses_versions(self, store):
        node = make_builder(store)._build_court_orders_node()
        # root -> special -> year -> case (leaf)
        special = node.children[0]
        year = special.children[0]
        case = year.children[0]
        assert len(case.manuscripts) == 1
        ms = case.manuscripts[0]
        assert ms.document_id == "ngm:court-order:special:081-CR-0081"
        assert ms.source_type == SourceType.COURT_ORDER.value
        raw = next(link for link in ms.links if link["role"] == "RAW")
        assert raw["link"].endswith("081-CR-0081.pdf")
        assert any(link["role"] == "ALTERNATE" for link in ms.links)
        assert_links_documentsource_compatible(ms.links)

    def test_full_tree_all_links_compatible(self, store):
        root = make_builder(store).build_tree()

        def walk(node):
            for ms in node.manuscripts:
                assert ms.document_id, "every indexed document needs a document_id"
                assert_links_documentsource_compatible(ms.links)
            for child in node.children:
                walk(child)

        walk(root)
        assert len(root.children) == 5
