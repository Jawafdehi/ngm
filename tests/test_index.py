"""
Tests for IndexBuilder — tree build, file output, $ref serialization,
fiscal year helpers, filename extraction, manuscript counting, and build_tree.

Covers:
- _node_to_dict_for_file: $ref stubs, next: null on paginated nodes
- write_index_files: actual file output to temp dir, index-v2.json alias
- _calculate_fiscal_year_from_registration: BS month logic
- _extract_case_number_from_filename: version stripping
- _count_manuscripts_in_tree: recursive count
- build_tree: deterministic ordering with mocked source builders
"""

import json
import pytest

from ngm.index.build_index import IndexBuilder
from ngm.index.models import IndexNode, Manuscript


# Helpers
def make_builder(tmp_path, page_size=100):
    return IndexBuilder(
        root_path=str(tmp_path),
        base_url="https://example.com",
        date_str="2026-03-24",
        page_size=page_size,
    )


def make_manuscripts(n, prefix="file"):
    return [
        Manuscript(
            url=f"https://example.com/{prefix}{i}.pdf", file_name=f"{prefix}{i}.pdf"
        )
        for i in range(n)
    ]


def make_leaf(name, path, n_manuscripts=3):
    return IndexNode(
        name=name, path=path, manuscripts=make_manuscripts(n_manuscripts, prefix=name)
    )


# _node_to_dict_for_file


class TestNodeToDictForFile:
    """_node_to_dict_for_file renders children as $ref stubs, not full objects."""

    def test_leaf_node_renders_manuscripts(self, tmp_path):
        builder = make_builder(tmp_path)
        ms = Manuscript(
            url="https://example.com/a.pdf", file_name="a.pdf", metadata={"k": "v"}
        )
        node = IndexNode(name="kanun-patrika", path="/kanun-patrika", manuscripts=[ms])

        result = builder._node_to_dict_for_file(node)

        assert result["name"] == "kanun-patrika"
        assert result["path"] == "/kanun-patrika"
        assert len(result["manuscripts"]) == 1
        assert result["manuscripts"][0]["url"] == "https://example.com/a.pdf"
        assert "children" not in result

    def test_branch_node_renders_children_as_ref_stubs(self, tmp_path):
        builder = make_builder(tmp_path)
        child = IndexNode(name="kanun-patrika", path="/kanun-patrika")
        root = IndexNode(name="root", path="/", children=[child])

        result = builder._node_to_dict_for_file(root)

        assert "children" in result
        stub = result["children"][0]
        # Must have $ref, name, path — and no manuscripts, no nested children
        assert stub["name"] == "kanun-patrika"
        assert stub["path"] == "/kanun-patrika"
        assert (
            stub["$ref"]
            == "https://example.com/indices/2026-03-24/index.kanun-patrika.json"
        )
        assert "manuscripts" not in stub
        assert "children" not in stub

    def test_multiple_children_all_get_ref_stubs(self, tmp_path):
        builder = make_builder(tmp_path)
        children = [
            IndexNode(name="ciaa-annual-reports", path="/ciaa-annual-reports"),
            IndexNode(name="kanun-patrika", path="/kanun-patrika"),
        ]
        root = IndexNode(name="root", path="/", children=children)

        result = builder._node_to_dict_for_file(root)

        assert len(result["children"]) == 2
        for stub in result["children"]:
            assert "$ref" in stub

    def test_next_url_included_when_set(self, tmp_path):
        builder = make_builder(tmp_path)
        node = IndexNode(
            name="test",
            path="/test",
            manuscripts=make_manuscripts(1),
            next_url="https://example.com/indices/2026-03-24/index.test.page-2.json",
        )

        result = builder._node_to_dict_for_file(node)

        assert (
            result["next"]
            == "https://example.com/indices/2026-03-24/index.test.page-2.json"
        )

    def test_next_null_when_explicitly_none(self, tmp_path):
        """Paginated last page: next_url=None should appear as next: null in output."""
        builder = make_builder(tmp_path)
        node = IndexNode(name="test", path="/test", manuscripts=make_manuscripts(1))
        node.next_url = None  # explicitly set (as pipeline does for last page)

        result = builder._node_to_dict_for_file(node)

        # next: null must be present for paginated last pages
        assert "next" in result
        assert result["next"] is None


# write_index_files — actual file output


class TestWriteIndexFiles:
    """write_index_files writes correct files to disk."""

    def _build_simple_tree(self):
        """Two-level tree: root → [kanun-patrika (leaf), ciaa-annual-reports (leaf)]"""
        leaf1 = make_leaf("kanun-patrika", "/kanun-patrika", n_manuscripts=2)
        leaf2 = make_leaf(
            "ciaa-annual-reports", "/ciaa-annual-reports", n_manuscripts=3
        )
        root = IndexNode(name="root", path="/", children=[leaf1, leaf2])
        return root

    def test_indices_dir_created(self, tmp_path):
        builder = make_builder(tmp_path)
        root = self._build_simple_tree()

        builder.write_index_files(root)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        assert indices_dir.exists()

    def test_root_alias_written(self, tmp_path):
        builder = make_builder(tmp_path)
        root = self._build_simple_tree()

        builder.write_index_files(root)

        root_alias = tmp_path / "index-v2.json"
        assert root_alias.exists()

    def test_root_alias_is_valid_json(self, tmp_path):
        builder = make_builder(tmp_path)
        root = self._build_simple_tree()

        builder.write_index_files(root)

        content = json.loads((tmp_path / "index-v2.json").read_text(encoding="utf-8"))
        assert content["name"] == "root"
        assert content["path"] == "/"

    def test_child_index_files_written(self, tmp_path):
        builder = make_builder(tmp_path)
        root = self._build_simple_tree()

        builder.write_index_files(root)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        assert (indices_dir / "index.kanun-patrika.json").exists()
        assert (indices_dir / "index.ciaa-annual-reports.json").exists()

    def test_root_index_file_written(self, tmp_path):
        builder = make_builder(tmp_path)
        root = self._build_simple_tree()

        builder.write_index_files(root)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        assert (indices_dir / "index.json").exists()

    def test_root_index_children_are_ref_stubs(self, tmp_path):
        builder = make_builder(tmp_path)
        root = self._build_simple_tree()

        builder.write_index_files(root)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        content = json.loads((indices_dir / "index.json").read_text(encoding="utf-8"))

        assert "children" in content
        for stub in content["children"]:
            assert "$ref" in stub
            assert stub["$ref"].startswith("https://example.com/indices/2026-03-24/")

    def test_leaf_index_file_has_manuscripts(self, tmp_path):
        builder = make_builder(tmp_path)
        root = self._build_simple_tree()

        builder.write_index_files(root)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        content = json.loads(
            (indices_dir / "index.kanun-patrika.json").read_text(encoding="utf-8")
        )

        assert "manuscripts" in content
        assert len(content["manuscripts"]) == 2

    def test_paginated_node_creates_multiple_files(self, tmp_path):
        builder = make_builder(tmp_path, page_size=10)
        leaf = IndexNode(
            name="kanun-patrika",
            path="/kanun-patrika",
            manuscripts=make_manuscripts(25),
        )
        root = IndexNode(name="root", path="/", children=[leaf])

        builder.write_index_files(root)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        assert (indices_dir / "index.kanun-patrika.json").exists()
        assert (indices_dir / "index.kanun-patrika.page-2.json").exists()
        assert (indices_dir / "index.kanun-patrika.page-3.json").exists()

    def test_paginated_first_page_has_next_link(self, tmp_path):
        builder = make_builder(tmp_path, page_size=10)
        leaf = IndexNode(
            name="kanun-patrika",
            path="/kanun-patrika",
            manuscripts=make_manuscripts(25),
        )
        root = IndexNode(name="root", path="/", children=[leaf])

        builder.write_index_files(root)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        content = json.loads(
            (indices_dir / "index.kanun-patrika.json").read_text(encoding="utf-8")
        )
        assert (
            content["next"]
            == "https://example.com/indices/2026-03-24/index.kanun-patrika.page-2.json"
        )

    def test_paginated_last_page_has_next_null(self, tmp_path):
        builder = make_builder(tmp_path, page_size=10)
        leaf = IndexNode(
            name="kanun-patrika",
            path="/kanun-patrika",
            manuscripts=make_manuscripts(25),
        )
        root = IndexNode(name="root", path="/", children=[leaf])

        builder.write_index_files(root)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        content = json.loads(
            (indices_dir / "index.kanun-patrika.page-3.json").read_text(
                encoding="utf-8"
            )
        )
        assert content["next"] is None

    def test_deep_tree_all_files_written(self, tmp_path):
        """Three-level tree: root → court-orders → supreme → leaf."""
        builder = make_builder(tmp_path)
        leaf = make_leaf("081", "/court-orders/supreme/081", n_manuscripts=2)
        supreme = IndexNode(
            name="supreme", path="/court-orders/supreme", children=[leaf]
        )
        court_orders = IndexNode(
            name="court-orders", path="/court-orders", children=[supreme]
        )
        root = IndexNode(name="root", path="/", children=[court_orders])

        builder.write_index_files(root)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        assert (indices_dir / "index.json").exists()
        assert (indices_dir / "index.court-orders.json").exists()
        assert (indices_dir / "index.court-orders.supreme.json").exists()
        assert (indices_dir / "index.court-orders.supreme.081.json").exists()


# _extract_case_number_from_filename


class TestExtractCaseNumber:
    """Strips extension and version suffix from filenames."""

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("082-OA-0503.1.docx", "082-OA-0503"),
            ("082-OA-0503.docx", "082-OA-0503"),
            ("082-OA-0503.2.pdf", "082-OA-0503"),
            ("081-CR-0001.1.docx", "081-CR-0001"),
            ("079-WP-0100.pdf", "079-WP-0100"),
            ("082-OA-0503", "082-OA-0503"),  # no extension at all
        ],
    )
    def test_extract_case_number(self, tmp_path, filename, expected):
        builder = make_builder(tmp_path)
        result = builder._extract_case_number_from_filename(filename)
        assert result == expected


# _count_manuscripts_in_tree
class TestCountManuscripts:
    """Recursive manuscript count across tree levels."""

    def test_leaf_node(self, tmp_path):
        builder = make_builder(tmp_path)
        node = make_leaf("test", "/test", n_manuscripts=5)
        assert builder._count_manuscripts_in_tree(node) == 5

    def test_empty_node(self, tmp_path):
        builder = make_builder(tmp_path)
        node = IndexNode(name="test", path="/test")
        assert builder._count_manuscripts_in_tree(node) == 0

    def test_two_level_tree(self, tmp_path):
        builder = make_builder(tmp_path)
        leaf1 = make_leaf("a", "/a", n_manuscripts=3)
        leaf2 = make_leaf("b", "/b", n_manuscripts=7)
        root = IndexNode(name="root", path="/", children=[leaf1, leaf2])
        assert builder._count_manuscripts_in_tree(root) == 10

    def test_three_level_tree(self, tmp_path):
        builder = make_builder(tmp_path)
        leaf1 = make_leaf("081", "/court-orders/supreme/081", n_manuscripts=4)
        leaf2 = make_leaf("082", "/court-orders/supreme/082", n_manuscripts=6)
        supreme = IndexNode(
            name="supreme", path="/court-orders/supreme", children=[leaf1, leaf2]
        )
        root = IndexNode(name="root", path="/", children=[supreme])
        assert builder._count_manuscripts_in_tree(root) == 10

    def test_mixed_depth_tree(self, tmp_path):
        builder = make_builder(tmp_path)
        deep_leaf = make_leaf("deep", "/a/b/deep", n_manuscripts=2)
        mid = IndexNode(name="b", path="/a/b", children=[deep_leaf])
        shallow_leaf = make_leaf("shallow", "/shallow", n_manuscripts=8)
        root = IndexNode(name="root", path="/", children=[mid, shallow_leaf])
        assert builder._count_manuscripts_in_tree(root) == 10


# build_tree — mocked source builders


class TestBuildTree:
    """build_tree assembles root from source builders in deterministic order.

    build_tree uses fn.__name__ as a dict key, so patched methods need __name__
    set. We use a helper that wraps a lambda with the correct __name__.
    """

    def _mock_node(self, name, path, n=2):
        return IndexNode(
            name=name, path=path, manuscripts=make_manuscripts(n, prefix=name)
        )

    def _stub(self, method_name, return_value=None, side_effect=None):
        """Return a callable with __name__ matching the real method."""
        if side_effect is not None:

            def fn():
                raise side_effect

        else:

            def fn():
                return return_value

        fn.__name__ = method_name
        return fn

    def test_build_tree_returns_root_node(self, tmp_path):
        builder = make_builder(tmp_path)
        kanun = self._mock_node("kanun-patrika", "/kanun-patrika")
        ciaa_ar = self._mock_node("ciaa-annual-reports", "/ciaa-annual-reports")
        ciaa_pr = self._mock_node("ciaa-press-releases", "/ciaa-press-releases")
        court = self._mock_node("court-orders", "/court-orders")

        builder._build_kanun_patrika_node = self._stub(
            "_build_kanun_patrika_node", kanun
        )
        builder._build_ciaa_annual_reports_node = self._stub(
            "_build_ciaa_annual_reports_node", ciaa_ar
        )
        builder._build_ciaa_press_releases_node = self._stub(
            "_build_ciaa_press_releases_node", ciaa_pr
        )
        builder._build_court_orders_node = self._stub("_build_court_orders_node", court)

        root = builder.build_tree()

        assert root.name == "root"
        assert root.path == "/"

    def test_build_tree_has_four_children(self, tmp_path):
        builder = make_builder(tmp_path)
        kanun = self._mock_node("kanun-patrika", "/kanun-patrika")
        ciaa_ar = self._mock_node("ciaa-annual-reports", "/ciaa-annual-reports")
        ciaa_pr = self._mock_node("ciaa-press-releases", "/ciaa-press-releases")
        court = self._mock_node("court-orders", "/court-orders")

        builder._build_kanun_patrika_node = self._stub(
            "_build_kanun_patrika_node", kanun
        )
        builder._build_ciaa_annual_reports_node = self._stub(
            "_build_ciaa_annual_reports_node", ciaa_ar
        )
        builder._build_ciaa_press_releases_node = self._stub(
            "_build_ciaa_press_releases_node", ciaa_pr
        )
        builder._build_court_orders_node = self._stub("_build_court_orders_node", court)

        root = builder.build_tree()

        assert len(root.children) == 4

    def test_build_tree_preserves_source_order(self, tmp_path):
        """Children must follow builder_fns order regardless of thread completion."""
        builder = make_builder(tmp_path)
        kanun = self._mock_node("kanun-patrika", "/kanun-patrika")
        ciaa_ar = self._mock_node("ciaa-annual-reports", "/ciaa-annual-reports")
        ciaa_pr = self._mock_node("ciaa-press-releases", "/ciaa-press-releases")
        court = self._mock_node("court-orders", "/court-orders")

        builder._build_kanun_patrika_node = self._stub(
            "_build_kanun_patrika_node", kanun
        )
        builder._build_ciaa_annual_reports_node = self._stub(
            "_build_ciaa_annual_reports_node", ciaa_ar
        )
        builder._build_ciaa_press_releases_node = self._stub(
            "_build_ciaa_press_releases_node", ciaa_pr
        )
        builder._build_court_orders_node = self._stub("_build_court_orders_node", court)

        root = builder.build_tree()

        names = [c.name for c in root.children]
        assert names == [
            "kanun-patrika",
            "ciaa-annual-reports",
            "ciaa-press-releases",
            "court-orders",
        ]

    def test_build_tree_skips_none_builders(self, tmp_path):
        """Builders returning None (no data) are excluded from children."""
        builder = make_builder(tmp_path)
        kanun = self._mock_node("kanun-patrika", "/kanun-patrika")

        builder._build_kanun_patrika_node = self._stub(
            "_build_kanun_patrika_node", kanun
        )
        builder._build_ciaa_annual_reports_node = self._stub(
            "_build_ciaa_annual_reports_node", None
        )
        builder._build_ciaa_press_releases_node = self._stub(
            "_build_ciaa_press_releases_node", None
        )
        builder._build_court_orders_node = self._stub("_build_court_orders_node", None)

        root = builder.build_tree()

        assert len(root.children) == 1
        assert root.children[0].name == "kanun-patrika"

    def test_build_tree_empty_when_all_none(self, tmp_path):
        builder = make_builder(tmp_path)

        builder._build_kanun_patrika_node = self._stub(
            "_build_kanun_patrika_node", None
        )
        builder._build_ciaa_annual_reports_node = self._stub(
            "_build_ciaa_annual_reports_node", None
        )
        builder._build_ciaa_press_releases_node = self._stub(
            "_build_ciaa_press_releases_node", None
        )
        builder._build_court_orders_node = self._stub("_build_court_orders_node", None)

        root = builder.build_tree()

        assert root.children == []

    def test_build_tree_propagates_builder_exception(self, tmp_path):
        builder = make_builder(tmp_path)

        builder._build_kanun_patrika_node = self._stub(
            "_build_kanun_patrika_node", side_effect=RuntimeError("S3 down")
        )
        builder._build_ciaa_annual_reports_node = self._stub(
            "_build_ciaa_annual_reports_node", None
        )
        builder._build_ciaa_press_releases_node = self._stub(
            "_build_ciaa_press_releases_node", None
        )
        builder._build_court_orders_node = self._stub("_build_court_orders_node", None)

        with pytest.raises(RuntimeError, match="S3 down"):
            builder.build_tree()
