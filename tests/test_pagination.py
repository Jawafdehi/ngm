"""
Tests for pagination logic in IndexBuilder.

Tests:
- Pagination when manuscripts > PAGE_SIZE
- Filename generation for paginated nodes
- Next link generation
- _collect_paginated_jobs() method
"""

import pytest
from ngm.index.build_index import IndexBuilder
from ngm.index.models import IndexNode, Manuscript


class TestPaginationLogic:
    """Tests for pagination logic."""

    def test_no_pagination_when_under_page_size(self, tmp_path):
        """Test no pagination when manuscripts <= page_size."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://example.com",
            date_str="2026-03-24",
            page_size=100,
        )

        # Create node with 50 manuscripts (under page_size)
        manuscripts = [
            Manuscript(url=f"https://example.com/file{i}.pdf", file_name=f"file{i}.pdf")
            for i in range(50)
        ]
        node = IndexNode(name="test", path="/test", manuscripts=manuscripts)

        # Collect write jobs
        indices_dir = tmp_path / "indices" / "2026-03-24"
        indices_dir.mkdir(parents=True)
        pending = []
        builder._collect_write_jobs(node, indices_dir, pending)

        # Should create only 1 file
        assert len(pending) == 1
        file_path, content = pending[0]
        assert file_path.name == "index.test.json"
        assert len(content["manuscripts"]) == 50
        assert "next" not in content

    def test_pagination_when_over_page_size(self, tmp_path):
        """Test pagination when manuscripts > page_size."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://example.com",
            date_str="2026-03-24",
            page_size=100,
        )

        # Create node with 250 manuscripts (over page_size)
        manuscripts = [
            Manuscript(url=f"https://example.com/file{i}.pdf", file_name=f"file{i}.pdf")
            for i in range(250)
        ]
        node = IndexNode(name="test", path="/test", manuscripts=manuscripts)

        # Collect write jobs
        indices_dir = tmp_path / "indices" / "2026-03-24"
        indices_dir.mkdir(parents=True)
        pending = []
        builder._collect_write_jobs(node, indices_dir, pending)

        # Should create 3 files (100 + 100 + 50)
        assert len(pending) == 3

    def test_first_page_no_suffix(self, tmp_path):
        """Test first page has no .page-N suffix."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://example.com",
            date_str="2026-03-24",
            page_size=100,
        )

        manuscripts = [
            Manuscript(url=f"https://example.com/file{i}.pdf", file_name=f"file{i}.pdf")
            for i in range(150)
        ]
        node = IndexNode(name="test", path="/test", manuscripts=manuscripts)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        indices_dir.mkdir(parents=True)
        pending = []
        builder._collect_write_jobs(node, indices_dir, pending)

        # First file should have no suffix
        first_file_path, _ = pending[0]
        assert first_file_path.name == "index.test.json"

    def test_subsequent_pages_have_suffix(self, tmp_path):
        """Test subsequent pages have .page-N suffix."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://example.com",
            date_str="2026-03-24",
            page_size=100,
        )

        manuscripts = [
            Manuscript(url=f"https://example.com/file{i}.pdf", file_name=f"file{i}.pdf")
            for i in range(250)
        ]
        node = IndexNode(name="test", path="/test", manuscripts=manuscripts)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        indices_dir.mkdir(parents=True)
        pending = []
        builder._collect_write_jobs(node, indices_dir, pending)

        # Check filenames
        assert pending[0][0].name == "index.test.json"
        assert pending[1][0].name == "index.test.page-2.json"
        assert pending[2][0].name == "index.test.page-3.json"

    def test_next_links_are_correct(self, tmp_path):
        """Test next links point to correct URLs."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://example.com",
            date_str="2026-03-24",
            page_size=100,
        )

        manuscripts = [
            Manuscript(url=f"https://example.com/file{i}.pdf", file_name=f"file{i}.pdf")
            for i in range(250)
        ]
        node = IndexNode(name="test", path="/test", manuscripts=manuscripts)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        indices_dir.mkdir(parents=True)
        pending = []
        builder._collect_write_jobs(node, indices_dir, pending)

        # First page should link to page 2
        _, content1 = pending[0]
        assert (
            content1["next"]
            == "https://example.com/indices/2026-03-24/index.test.page-2.json"
        )

        # Second page should link to page 3
        _, content2 = pending[1]
        assert (
            content2["next"]
            == "https://example.com/indices/2026-03-24/index.test.page-3.json"
        )

    def test_last_page_has_next_null(self, tmp_path):
        """Test last page has next: null."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://example.com",
            date_str="2026-03-24",
            page_size=100,
        )

        manuscripts = [
            Manuscript(url=f"https://example.com/file{i}.pdf", file_name=f"file{i}.pdf")
            for i in range(250)
        ]
        node = IndexNode(name="test", path="/test", manuscripts=manuscripts)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        indices_dir.mkdir(parents=True)
        pending = []
        builder._collect_write_jobs(node, indices_dir, pending)

        # Last page should have next: null
        _, last_content = pending[-1]
        assert last_content["next"] is None

    def test_page_manuscript_counts(self, tmp_path):
        """Test each page has correct number of manuscripts."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://example.com",
            date_str="2026-03-24",
            page_size=100,
        )

        manuscripts = [
            Manuscript(url=f"https://example.com/file{i}.pdf", file_name=f"file{i}.pdf")
            for i in range(250)
        ]
        node = IndexNode(name="test", path="/test", manuscripts=manuscripts)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        indices_dir.mkdir(parents=True)
        pending = []
        builder._collect_write_jobs(node, indices_dir, pending)

        # First two pages should have 100 manuscripts each
        _, content1 = pending[0]
        assert len(content1["manuscripts"]) == 100

        _, content2 = pending[1]
        assert len(content2["manuscripts"]) == 100

        # Last page should have 50 manuscripts
        _, content3 = pending[2]
        assert len(content3["manuscripts"]) == 50

    def test_pagination_with_exact_page_size(self, tmp_path):
        """Test pagination when manuscripts exactly equals page_size."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://example.com",
            date_str="2026-03-24",
            page_size=100,
        )

        manuscripts = [
            Manuscript(url=f"https://example.com/file{i}.pdf", file_name=f"file{i}.pdf")
            for i in range(100)
        ]
        node = IndexNode(name="test", path="/test", manuscripts=manuscripts)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        indices_dir.mkdir(parents=True)
        pending = []
        builder._collect_write_jobs(node, indices_dir, pending)

        # Should create only 1 file (no pagination)
        assert len(pending) == 1
        _, content = pending[0]
        assert len(content["manuscripts"]) == 100
        assert "next" not in content

    def test_pagination_with_one_over_page_size(self, tmp_path):
        """Test pagination when manuscripts = page_size + 1."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://example.com",
            date_str="2026-03-24",
            page_size=100,
        )

        manuscripts = [
            Manuscript(url=f"https://example.com/file{i}.pdf", file_name=f"file{i}.pdf")
            for i in range(101)
        ]
        node = IndexNode(name="test", path="/test", manuscripts=manuscripts)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        indices_dir.mkdir(parents=True)
        pending = []
        builder._collect_write_jobs(node, indices_dir, pending)

        # Should create 2 files (100 + 1)
        assert len(pending) == 2

        _, content1 = pending[0]
        assert len(content1["manuscripts"]) == 100
        assert (
            content1["next"]
            == "https://example.com/indices/2026-03-24/index.test.page-2.json"
        )

        _, content2 = pending[1]
        assert len(content2["manuscripts"]) == 1
        assert content2["next"] is None


class TestCollectPaginatedJobs:
    """Tests for _collect_paginated_jobs() method."""

    def test_collect_paginated_jobs_creates_correct_pages(self, tmp_path):
        """Test _collect_paginated_jobs creates correct number of pages."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://example.com",
            date_str="2026-03-24",
            page_size=50,
        )

        manuscripts = [
            Manuscript(url=f"https://example.com/file{i}.pdf", file_name=f"file{i}.pdf")
            for i in range(125)
        ]
        node = IndexNode(name="test", path="/test", manuscripts=manuscripts)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        indices_dir.mkdir(parents=True)
        pending = []

        # Calculate total pages
        total_pages = (len(manuscripts) + builder.page_size - 1) // builder.page_size
        assert total_pages == 3

        # Call _collect_paginated_jobs directly
        builder._collect_paginated_jobs(node, indices_dir, total_pages, pending)

        assert len(pending) == 3

    def test_collect_paginated_jobs_manuscript_distribution(self, tmp_path):
        """Test manuscripts are correctly distributed across pages."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://example.com",
            date_str="2026-03-24",
            page_size=50,
        )

        manuscripts = [
            Manuscript(
                url=f"https://example.com/file{i}.pdf",
                file_name=f"file{i}.pdf",
                metadata={"index": i},
            )
            for i in range(125)
        ]
        node = IndexNode(name="test", path="/test", manuscripts=manuscripts)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        indices_dir.mkdir(parents=True)
        pending = []
        total_pages = 3

        builder._collect_paginated_jobs(node, indices_dir, total_pages, pending)

        # Check first page has manuscripts 0-49
        _, content1 = pending[0]
        assert content1["manuscripts"][0]["metadata"]["index"] == 0
        assert content1["manuscripts"][-1]["metadata"]["index"] == 49

        # Check second page has manuscripts 50-99
        _, content2 = pending[1]
        assert content2["manuscripts"][0]["metadata"]["index"] == 50
        assert content2["manuscripts"][-1]["metadata"]["index"] == 99

        # Check third page has manuscripts 100-124
        _, content3 = pending[2]
        assert content3["manuscripts"][0]["metadata"]["index"] == 100
        assert content3["manuscripts"][-1]["metadata"]["index"] == 124

    def test_collect_paginated_jobs_preserves_node_metadata(self, tmp_path):
        """Test paginated nodes preserve name and path."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://example.com",
            date_str="2026-03-24",
            page_size=50,
        )

        manuscripts = [
            Manuscript(url=f"https://example.com/file{i}.pdf", file_name=f"file{i}.pdf")
            for i in range(100)
        ]
        node = IndexNode(
            name="ciaa-reports", path="/ciaa-reports", manuscripts=manuscripts
        )

        indices_dir = tmp_path / "indices" / "2026-03-24"
        indices_dir.mkdir(parents=True)
        pending = []
        total_pages = 2

        builder._collect_paginated_jobs(node, indices_dir, total_pages, pending)

        # All pages should have same name and path
        for _, content in pending:
            assert content["name"] == "ciaa-reports"
            assert content["path"] == "/ciaa-reports"


class TestPaginationWithDifferentPageSizes:
    """Tests pagination with various page sizes."""

    @pytest.mark.parametrize(
        "total_manuscripts,page_size,expected_pages",
        [
            (50, 100, 1),  # Under page size
            (100, 100, 1),  # Exactly page size
            (101, 100, 2),  # One over
            (200, 100, 2),  # Exactly 2 pages
            (250, 100, 3),  # 2.5 pages
            (1000, 100, 10),  # Many pages
            (10, 5, 2),  # Small page size
            (99, 10, 10),  # Edge case
        ],
    )
    def test_pagination_page_count(
        self, tmp_path, total_manuscripts, page_size, expected_pages
    ):
        """Test correct number of pages for various manuscript counts."""
        builder = IndexBuilder(
            root_path=str(tmp_path),
            base_url="https://example.com",
            date_str="2026-03-24",
            page_size=page_size,
        )

        manuscripts = [
            Manuscript(url=f"https://example.com/file{i}.pdf", file_name=f"file{i}.pdf")
            for i in range(total_manuscripts)
        ]
        node = IndexNode(name="test", path="/test", manuscripts=manuscripts)

        indices_dir = tmp_path / "indices" / "2026-03-24"
        indices_dir.mkdir(parents=True)
        pending = []
        builder._collect_write_jobs(node, indices_dir, pending)

        assert len(pending) == expected_pages
