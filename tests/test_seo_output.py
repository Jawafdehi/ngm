"""
Tests for the SEO surface emitted by IndexBuilder.write_seo_files:
crawlable HTML landing pages, a sitemap index, per-dataset child sitemaps
(paginated at the 50k-URL limit), and robots.txt.
"""

import xml.etree.ElementTree as ET


from ngm.index import build_index as bi
from ngm.index.build_index import IndexBuilder
from ngm.index.models import IndexNode, Manuscript

SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def make_builder(tmp_path):
    return IndexBuilder(
        root_path=str(tmp_path),
        base_url="https://ngm-store.example.org",
        date_str="2026-06-26",
    )


def make_doc(doc_id, name="a.pdf", links=None, metadata=None):
    raw = f"https://ngm-store.example.org/uploads/{name}"
    return Manuscript(
        url=raw,
        file_name=name,
        metadata=metadata or {},
        links=links or [{"link": raw, "role": "RAW"}],
        document_id=doc_id,
        source_type="MISC",
    )


def simple_tree():
    pr = IndexNode(
        name="ciaa-press-releases",
        path="/ciaa-press-releases",
        manuscripts=[
            make_doc(
                "ngm:ciaa-press-release:1234",
                name="charge.pdf",
                links=[
                    {
                        "link": "https://ngm-store.example.org/uploads/charge.pdf",
                        "role": "RAW",
                    },
                    {
                        "link": "https://ciaa.gov.np/pressrelease/1234",
                        "role": "SOURCE_PAGE",
                    },
                ],
                metadata={
                    "title": "मुद्दा",
                    "full_text": "विवरण",
                    "publication_date": "2082-12-08",
                },
            )
        ],
    )
    leaf = IndexNode(
        name="081-CR-0081",
        path="/court-orders/special/081/081-CR-0081",
        manuscripts=[
            make_doc("ngm:court-order:special:081-CR-0081", name="081-CR-0081.pdf")
        ],
    )
    year = IndexNode(name="081", path="/court-orders/special/081", children=[leaf])
    special = IndexNode(name="special", path="/court-orders/special", children=[year])
    court = IndexNode(name="court-orders", path="/court-orders", children=[special])
    return IndexNode(name="root", path="/", children=[pr, court])


class TestSeoFiles:
    def test_writes_html_sitemaps_and_robots(self, tmp_path):
        make_builder(tmp_path).write_seo_files(simple_tree())

        # HTML landing pages keyed by document_id under /d/
        assert (tmp_path / "d/ngm/ciaa-press-release/1234.html").exists()
        assert (tmp_path / "d/ngm/court-order/special/081-CR-0081.html").exists()

        # sitemap index + per-dataset child sitemaps + robots
        assert (tmp_path / "sitemap.xml").exists()
        assert (tmp_path / "sitemap.ciaa-press-releases.xml").exists()
        assert (tmp_path / "sitemap.court-orders.xml").exists()
        assert (tmp_path / "robots.txt").exists()

    def test_robots_points_at_sitemap(self, tmp_path):
        make_builder(tmp_path).write_seo_files(simple_tree())
        robots = (tmp_path / "robots.txt").read_text(encoding="utf-8")
        assert "Sitemap: https://ngm-store.example.org/sitemap.xml" in robots
        assert "Allow: /" in robots

    def test_sitemap_index_is_valid_and_references_children(self, tmp_path):
        make_builder(tmp_path).write_seo_files(simple_tree())
        root = ET.fromstring((tmp_path / "sitemap.xml").read_text(encoding="utf-8"))
        assert root.tag == f"{SM_NS}sitemapindex"
        locs = {el.text for el in root.iter(f"{SM_NS}loc")}
        assert "https://ngm-store.example.org/sitemap.ciaa-press-releases.xml" in locs
        assert "https://ngm-store.example.org/sitemap.court-orders.xml" in locs

    def test_child_sitemap_lists_landing_pages(self, tmp_path):
        make_builder(tmp_path).write_seo_files(simple_tree())
        root = ET.fromstring(
            (tmp_path / "sitemap.ciaa-press-releases.xml").read_text(encoding="utf-8")
        )
        assert root.tag == f"{SM_NS}urlset"
        locs = {el.text for el in root.iter(f"{SM_NS}loc")}
        assert (
            "https://ngm-store.example.org/d/ngm/ciaa-press-release/1234.html" in locs
        )

    def test_html_has_canonical_jsonld_and_links(self, tmp_path):
        make_builder(tmp_path).write_seo_files(simple_tree())
        page = (tmp_path / "d/ngm/ciaa-press-release/1234.html").read_text(
            encoding="utf-8"
        )
        assert 'lang="ne"' in page
        assert (
            '<link rel="canonical" href="https://ngm-store.example.org/d/ngm/ciaa-press-release/1234.html">'
            in page
        )
        assert "application/ld+json" in page
        assert '"@type": "CreativeWork"' in page
        assert "https://ciaa.gov.np/pressrelease/1234" in page  # SOURCE_PAGE link
        assert "charge.pdf" in page  # RAW download link

    def test_sitemap_pagination_splits_at_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bi, "SITEMAP_MAX_URLS", 2)
        docs = [make_doc(f"ngm:kanun-patrika:k{i}", name=f"k{i}.pdf") for i in range(5)]
        root = IndexNode(
            name="root",
            path="/",
            children=[
                IndexNode(name="kanun-patrika", path="/kanun-patrika", manuscripts=docs)
            ],
        )

        make_builder(tmp_path).write_seo_files(root)

        # 5 docs / 2 per file => 3 child sitemaps
        assert (tmp_path / "sitemap.kanun-patrika.xml").exists()
        assert (tmp_path / "sitemap.kanun-patrika.page-2.xml").exists()
        assert (tmp_path / "sitemap.kanun-patrika.page-3.xml").exists()

        index = ET.fromstring((tmp_path / "sitemap.xml").read_text(encoding="utf-8"))
        child_locs = [el.text for el in index.iter(f"{SM_NS}loc")]
        assert len(child_locs) == 3
        # Every listed page sitemap must stay within the limit.
        for child in child_locs:
            fname = child.rsplit("/", 1)[-1]
            urlset = ET.fromstring((tmp_path / fname).read_text(encoding="utf-8"))
            assert len(list(urlset.iter(f"{SM_NS}url"))) <= 2

    def test_empty_tree_writes_nothing(self, tmp_path):
        make_builder(tmp_path).write_seo_files(IndexNode(name="root", path="/"))
        assert not (tmp_path / "sitemap.xml").exists()
        assert not (tmp_path / "robots.txt").exists()
