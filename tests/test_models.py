"""
Tests for ngm.index.models module.

Tests Manuscript and IndexNode dataclasses including:
- Serialization (to_dict/from_dict)
- Validation rules
- Type checking methods
"""

import pytest
from ngm.index.models import Manuscript, IndexNode


class TestManuscript:
    """Tests for Manuscript dataclass."""

    def test_create_manuscript_minimal(self):
        """Test for creating manuscript with minimal fields"""
        ms = Manuscript(url="https://abc.com/file.pdf", file_name="file.pdf")
        assert ms.url == "https://abc.com/file.pdf"
        assert ms.file_name == "file.pdf"
        assert ms.metadata == {}

    def test_create_manuscript_with_metadata(self):
        """Test to create manuscript with metadata field"""
        metadata = {"title": "Title 1", "year": "2080"}
        ms = Manuscript(
            url="https://abc.com/file.pdf", file_name="file.pdf", metadata=metadata
        )
        assert ms.metadata == metadata

    def test_manuscript_to_dict(self):
        """Test manuscript to dict,serialization"""
        metadata = {"title": "Title 1"}
        ms = Manuscript(
            url="https://abc.com/file.pdf", file_name="file.pdf", metadata=metadata
        )
        result = ms.to_dict()

        assert result == {
            "url": "https://abc.com/file.pdf",
            "file_name": "file.pdf",
            "metadata": {"title": "Title 1"},
        }

    def test_manuscript_from_dict(self):
        """Test manuscript deserialization from dict"""
        data = {
            "url": "https://abc.com/file.pdf",
            "file_name": "file_pdf",
            "metadata": {"title": "Title 1"},
        }
        ms = Manuscript.from_dict(data)

        assert ms.url == "https://abc.com/file.pdf"
        assert ms.file_name == "file_pdf"
        assert ms.metadata == {"title": "Title 1"}

    def test_manuscript_round_trip(self):
        """Test manuscript serialization round trip"""
        metadata = {"title": "Title 1"}
        ms = Manuscript(
            url="https://abc.com/file.pdf", file_name="file_pdf", metadata=metadata
        )

        # Serialize and deserialize
        serliazed_data = ms.to_dict()
        deserialized_data = Manuscript.from_dict(serliazed_data)

        assert deserialized_data.url == "https://abc.com/file.pdf"
        assert deserialized_data.file_name == "file_pdf"
        assert deserialized_data.metadata == {"title": "Title 1"}

    def test_manuscript_from_dict_no_metadata(self):
        """Test manuscript deserialization when metadata is missing"""
        data = {
            "url": "https://abc.com/file.pdf",
            "file_name": "file_pdf",
        }
        ms = Manuscript.from_dict(data)

        assert ms.url == "https://abc.com/file.pdf"
        assert ms.file_name == "file_pdf"
        assert ms.metadata == {}


class TestIndexNode:
    """Tests for IndexNode dataclass"""

    def test_create_node_minimal(self):
        """Test creating node with required fields only"""
        node = IndexNode(name="test", path="/test")
        assert node.name == "test"
        assert node.path == "/test"
        assert node.children == []
        assert node.manuscripts == []
        assert node.ref is None
        assert node.next_url is None

    def test_is_ref(self):
        """Test the is_ref() method"""
        node_with_ref = IndexNode(name="test", path="/test", ref="https://abc.com")
        node_without_ref = IndexNode(name="test", path="/test")
        assert node_with_ref.is_ref() is True
        assert node_without_ref.is_ref() is False

    def test_is_leaf(self):
        """Test the is_leaf() method"""
        leaf_node = IndexNode(name="test", path="/test")
        branch_node = IndexNode(
            name="test",
            path="/test",
            children=[IndexNode(name="child", path="/test/child")],
        )
        ref_node = IndexNode(name="test", path="/test", ref="https://abc.com")
        assert leaf_node.is_leaf() is True
        assert branch_node.is_leaf() is False
        assert ref_node.is_leaf() is False

    def test_node_is_branch(self):
        """Test is_branch() method."""
        branch_node = IndexNode(
            name="test",
            path="/test",
            children=[IndexNode(name="child", path="/test/child")],
        )
        leaf_node = IndexNode(name="test", path="/test")
        assert branch_node.is_branch() is True
        assert leaf_node.is_branch() is False

    def test_leaf_node_to_dict(self):
        """Test leaf node serialization"""
        ms = Manuscript(url="https://abc.com/file.pdf", file_name="file.pdf")
        node = IndexNode(name="test", path="/test", manuscripts=[ms])

        result = node.to_dict()

        assert result["name"] == "test"
        assert result["path"] == "/test"
        assert "manuscripts" in result
        assert len(result["manuscripts"]) == 1
        assert "children" not in result
        assert "$ref" not in result

    def test_branch_node_to_dict(self):
        """Test branch node serialization."""
        child = IndexNode(name="child", path="/test/child")
        node = IndexNode(name="test", path="/test", children=[child])

        result = node.to_dict()

        assert result["name"] == "test"
        assert result["path"] == "/test"
        assert "children" in result
        assert len(result["children"]) == 1
        assert "manuscripts" not in result
        assert "$ref" not in result

    def test_ref_node_to_dict(self):
        """Test ref node serialization."""
        node = IndexNode(name="test", path="/test", ref="https://abc.com/index.json")
        result = node.to_dict()
        assert result["name"] == "test"
        assert result["path"] == "/test"
        assert result["$ref"] == "https://abc.com/index.json"
        assert "children" not in result
        assert "manuscripts" not in result
        assert "next" not in result

    def test_node_to_dict_with_next(self):
        """Test node serialization with next_url."""
        node = IndexNode(
            name="test", path="/test", next_url="https://abc.com/page2.json"
        )
        result = node.to_dict()
        assert result["next"] == "https://abc.com/page2.json"

    def test_node_from_dict_leaf(self):
        """Test deserialization of leaf node"""
        data = {
            "name": "test",
            "path": "/test",
            "manuscripts": [
                {
                    "url": "https://abc.com/file.pdf",
                    "file_name": "file.pdf",
                    "metadata": {},
                }
            ],
        }

        node = IndexNode.from_dict(data)

        assert node.name == "test"
        assert node.path == "/test"
        assert len(node.manuscripts) == 1
        assert node.manuscripts[0].url == "https://abc.com/file.pdf"

    def test_node_from_dict_branch(self):
        """Test branch node deserialization."""
        data = {
            "name": "test",
            "path": "/test",
            "children": [{"name": "child", "path": "/test/child"}],
        }
        node = IndexNode.from_dict(data)
        assert node.name == "test"
        assert len(node.children) == 1
        assert node.children[0].name == "child"

    def test_node_round_trip(self):
        """Test node serialization round-trip."""
        ms = Manuscript(url="https://abc.com/file.pdf", file_name="file.pdf")
        node = IndexNode(name="test", path="/test", manuscripts=[ms])

        serialized_data = node.to_dict()
        deserialized_data = IndexNode.from_dict(serialized_data)

        assert deserialized_data.name == node.name
        assert deserialized_data.path == node.path
        assert len(deserialized_data.manuscripts) == len(node.manuscripts)

    def test_validate_ref_with_children_fails(self):
        """Test validation fails when ref node has children."""
        child = IndexNode(name="child", path="/test/child")
        node = IndexNode(
            name="test",
            path="/test",
            ref="https://abc.com",
            children=[child],
        )
        with pytest.raises(ValueError, match="has ref set but also has children"):
            node.validate()

    def test_validate_ref_with_manuscripts_fails(self):
        """Test validation fails when ref node has manuscripts."""
        ms = Manuscript(url="https://abc.com/file.pdf", file_name="file.pdf")
        node = IndexNode(
            name="test",
            path="/test",
            ref="https://abc.com",
            manuscripts=[ms],
        )
        with pytest.raises(ValueError, match="has ref set but also has"):
            node.validate()

    def test_validate_ref_with_next_url_fails(self):
        """Test validation fails when ref node has next_url."""
        node = IndexNode(
            name="test",
            path="/test",
            ref="https://abc.com",
            next_url="https://abc.com/page2",
        )
        with pytest.raises(ValueError, match="has ref set but also has"):
            node.validate()

    def test_validate_children_and_manuscripts_fails(self):
        """Test validation fails when node has both children and manuscripts."""
        child = IndexNode(name="child", path="/test/child")
        ms = Manuscript(url="https://abc.com/file.pdf", file_name="file.pdf")
        node = IndexNode(
            name="test",
            path="/test",
            children=[child],
            manuscripts=[ms],
        )

        with pytest.raises(ValueError, match="has both children and manuscripts"):
            node.validate()

    def test_validate_recursive(self):
        """Test validation is recursive for children."""
        # Create invalid child x
        invalid_child = IndexNode(
            name="child",
            path="/test/child",
            ref="https://abc.com",
            manuscripts=[
                Manuscript(url="https://abc.com/file.pdf", file_name="file.pdf")
            ],
        )

        # Parent node is valid, but child is not
        parent = IndexNode(name="parent", path="/parent", children=[invalid_child])

        with pytest.raises(ValueError):
            parent.validate()

    def test_validate_passes_for_valid_tree(self):
        """Test validation passes for valid tree structure."""
        leaf = IndexNode(
            name="leaf",
            path="/parent/leaf",
            manuscripts=[
                Manuscript(url="https://abc.com/file.pdf", file_name="file.pdf")
            ],
        )

        parent = IndexNode(
            name="parent", path="/parent", children=[leaf]
        )  # Should not raise
        parent.validate()
