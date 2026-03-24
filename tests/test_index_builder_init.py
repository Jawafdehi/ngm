"""
Tests for IndexBuilder initialization and helper methods.

Tests:
- Constructor validation
- Path helper methods
- URL construction
"""

import pytest
from pathlib import Path
from ngm.index.build_index import IndexBuilder
from ngm.index.models import IndexNode


class TestIndexBuilderInit:
    """Tests for IndexBuilder constructor."""

    def test_init_valid(self, tmp_path):
        """Test valid initialization."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://abc.com",
            date_str="2026-03-24",
            page_size=100,
        )

        assert builder.root_path == tmp_path
        assert builder.base_url == "https://abc.com"
        assert builder.date_str == "2026-03-24"
        assert builder.page_size == 100
        assert builder.indices_base_url == "https://abc.com/indices/2026-03-24"

    def test_init_strips_trailing_slash(self, tmp_path):
        """Test base_url trailing slash is stripped."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://abc.com/",
            date_str="2026-03-24",
        )

        assert builder.base_url == "https://abc.com"

    def test_init_invalid_page_size_zero(self, tmp_path):
        """Test initialization fails with page_size=0."""
        with pytest.raises(ValueError, match="page_size must be > 0"):
            IndexBuilder(
                root_path=str(tmp_path),
                base_url="https://abc.com",
                date_str="2026-03-24",
                page_size=0,
            )

    def test_init_invalid_page_size_negative(self, tmp_path):
        """Test initialization fails with negative page_size."""
        with pytest.raises(ValueError, match="page_size must be > 0"):
            IndexBuilder(
                root_path=str(tmp_path),
                base_url="https://abc.com",
                date_str="2026-03-24",
                page_size=-10,
            )

    def test_init_invalid_page_size_non_integer(self, tmp_path):
        """Test initialization fails with non-integer page_size."""
        with pytest.raises(ValueError, match="page_size must be a valid integer"):
            IndexBuilder(
                root_path=str(tmp_path),
                base_url="https://abc.com",
                date_str="2026-03-24",
                page_size="not_a_number",
            )

    def test_init_page_size_string_integer(self, tmp_path):
        """Test initialization converts string integer to int."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://abc.com",
            date_str="2026-03-24",
            page_size="50",
        )

        assert builder.page_size == 50
        assert isinstance(builder.page_size, int)


class TestIndexBuilderPathHelpers:
    """Tests for IndexBuilder path helper methods."""

    def test_build_folder_structure(self, tmp_path):
        """Test _build_folder_structure constructs correct paths."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://abc.com",
            date_str="2026-03-24",
        )

        result = builder._build_folder_structure("supreme-court", "kanun-patrika")
        expected = tmp_path / "uploads" / "supreme-court" / "kanun-patrika"

        assert result == expected

    def test_relative_path(self, tmp_path):
        """Test _relative_path returns correct relative path."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://abc.com",
            date_str="2026-03-24",
        )

        file_path = tmp_path / "uploads" / "test" / "file.pdf"
        result = builder._relative_path(file_path)

        assert result == "uploads/test/file.pdf"

    def test_relative_path_outside_root_fails(self, tmp_path):
        """Test _relative_path raises ValueError for paths outside root."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://abc.com",
            date_str="2026-03-24",
        )

        outside_path = Path("/some/different/path/file.pdf")

        with pytest.raises(ValueError, match="is outside root_path"):
            builder._relative_path(outside_path)

    def test_build_url(self, tmp_path):
        """Test _build_url constructs correct full URL."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://abc.com",
            date_str="2026-03-24",
        )

        file_path = tmp_path / "uploads" / "test" / "file.pdf"
        result = builder._build_url(file_path)

        assert result == "https://abc.com/uploads/test/file.pdf"

    def test_build_url_with_special_characters(self, tmp_path):
        """Test _build_url handles special characters."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://abc.com",
            date_str="2026-03-24",
        )

        file_path = tmp_path / "uploads" / "test file" / "document.pdf"
        result = builder._build_url(file_path)

        # Should preserve spaces (URL encoding happens at HTTP level)
        assert "test file" in result


class TestIndexBuilderFilenameGeneration:
    """Tests for filename generation methods."""

    def test_node_filename_root(self, tmp_path):
        """Test _node_filename for root node."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://abc.com",
            date_str="2026-03-24",
        )

        node = IndexNode(name="root", path="/")

        result = builder._node_filename(node)
        assert result == "index.json"

    def test_node_filename_single_level(self, tmp_path):
        """Test _node_filename for single-level path."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://abc.com",
            date_str="2026-03-24",
        )

        from ngm.index.models import IndexNode

        node = IndexNode(name="kanun-patrika", path="/kanun-patrika")

        result = builder._node_filename(node)
        assert result == "index.kanun-patrika.json"

    def test_node_filename_multi_level(self, tmp_path):
        """Test _node_filename for multi-level path."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://abc.com",
            date_str="2026-03-24",
        )

        from ngm.index.models import IndexNode

        node = IndexNode(name="081", path="/court-orders/supreme/081")

        result = builder._node_filename(node)
        assert result == "index.court-orders.supreme.081.json"

    def test_node_ref_url(self, tmp_path):
        """Test _node_ref_url generates correct URL."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://abc.com",
            date_str="2026-03-24",
        )

        from ngm.index.models import IndexNode

        node = IndexNode(name="kanun-patrika", path="/kanun-patrika")

        result = builder._node_ref_url(node)
        assert result == "https://abc.com/indices/2026-03-24/index.kanun-patrika.json"
