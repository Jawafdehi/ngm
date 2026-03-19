"""
Data models for NGM Index v2.0.

This module defines the core data structures for the new hierarchical index system:
- Manuscript: Represents a single document/file with metadata
- IndexNode: Represents a node in the hierarchical index tree
"""

from typing import Any
from dataclasses import dataclass, field


@dataclass
class Manuscript:
    """Represents a single document/manuscript in the index."""

    url: str
    file_name: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert manuscript to dictionary for JSON serialization."""
        return {
            "url": self.url,
            "file_name": self.file_name,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manuscript":
        """Create manuscript from dictionary."""
        return cls(
            url=data["url"],
            file_name=data["file_name"],
            metadata=data.get("metadata", {}),
        )


@dataclass
class IndexNode:
    """Represents a node in the hierarchical index tree."""

    name: str
    path: str
    children: list["IndexNode"] = field(default_factory=list)
    manuscripts: list[Manuscript] = field(default_factory=list)
    ref: str | None = None  # External reference URL
    next_url: str | None = None  # Pagination link

    def is_ref(self) -> bool:
        """Check if this is a reference node (points to another index file)."""
        return self.ref is not None

    def is_leaf(self) -> bool:
        """Check if this is a leaf node (has manuscripts, no children or ref)."""
        return self.ref is None and len(self.children) == 0

    def is_branch(self) -> bool:
        """Check if this is a branch node (has children, no manuscripts or ref)."""
        return len(self.children) > 0

    def to_dict(self) -> dict[str, Any]:
        """Convert node to dictionary for JSON serialization."""
        result = {
            "name": self.name,
            "path": self.path,
        }

        if self.ref:
            result["$ref"] = self.ref
            return result  # Early return; ref nodes have nothing else

        if self.children:
            result["children"] = [child.to_dict() for child in self.children]

        if self.manuscripts:
            result["manuscripts"] = [ms.to_dict() for ms in self.manuscripts]

        if self.next_url:
            result["next"] = self.next_url

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IndexNode":
        """Create node from dictionary."""
        node = cls(
            name=data["name"],
            path=data["path"],
            ref=data.get("$ref"),
            next_url=data.get("next"),
        )

        if "children" in data:
            node.children = [
                cls.from_dict(child_data) for child_data in data["children"]
            ]

        if "manuscripts" in data:
            node.manuscripts = [
                Manuscript.from_dict(ms_data) for ms_data in data["manuscripts"]
            ]

        # Validate after construction
        node.validate()
        return node

    def validate(self) -> None:
        """Validate node structure and constraints."""
        # Mutual exclusivity: ref nodes cannot have children, manuscripts, or next_url
        if self.ref and (self.children or self.manuscripts or self.next_url):
            raise ValueError(
                f"Node '{self.name}' has ref set but also has children/manuscripts/next_url"
            )

        # A node shouldn't have both children and manuscripts
        if self.children and self.manuscripts:
            raise ValueError(f"Node '{self.name}' has both children and manuscripts")

        # Recursively validate children
        for child in self.children:
            child.validate()
