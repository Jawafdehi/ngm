"""
Data models for NGM Index v2.0.

This module defines the core data structures for the new hierarchical index system:
- Manuscript: Represents a single document/file with metadata
- IndexNode: Represents a node in the hierarchical index tree

A Manuscript also carries a roled link list (``links``) and a stable
``document_id`` so that every NGM document mirrors the Jawafdehi API's
``DocumentSource`` shape field-for-field. This is deliberate: it lets a future
importer map one manuscript to one ``DocumentSource`` with no transformation
(``links`` → ``url``, ``document_id`` → external ref). NGM is NOT coupled to the
API today — it only adopts the format.
"""

from enum import StrEnum
from typing import Any
from dataclasses import dataclass, field


class SourceLinkRole(StrEnum):
    """Role of a single document link.

    Mirrors ``cases.models.SourceLinkRole`` in jawafdehi-api exactly so a
    manuscript's ``links`` list is a drop-in for ``DocumentSource.url``.
    """

    RAW = "RAW"  # The original document file (PDF/DOC)
    MARKDOWN = "MARKDOWN"  # A markdown transcript of the document
    PERMALINK = "PERMALINK"  # A permanent canonical link
    SOURCE_PAGE = "SOURCE_PAGE"  # The web page the document was published on
    ALTERNATE = "ALTERNATE"  # An alternate-format rendering of the RAW file


class SourceType(StrEnum):
    """Kind of document, mirroring ``cases.models.SourceType`` in jawafdehi-api.

    Carried as a best-effort hint only; final classification happens at import
    time via ``cases.services.source_classifier``.
    """

    CIAA_PRESS_RELEASE = "CIAA_PRESS_RELEASE"
    AG_ABHIYOG_PATRA = "AG_ABHIYOG_PATRA"
    OAG_AUDIT_REPORT = "OAG_AUDIT_REPORT"
    COURT_ORDER = "COURT_ORDER"
    COURT_FILING_OTHER = "COURT_FILING_OTHER"
    LAW_OR_BILL = "LAW_OR_BILL"
    NEWS = "NEWS"
    SOCIAL_MEDIA = "SOCIAL_MEDIA"
    MISC = "MISC"


@dataclass
class Manuscript:
    """Represents a single logical document/manuscript in the index.

    ``url``/``file_name`` carry the primary RAW file for backward compatibility
    with existing consumers; ``links`` is the full roled link list (RAW file,
    ALTERNATE renderings, SOURCE_PAGE, and a MARKDOWN transcript once produced).
    """

    url: str
    file_name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    # Roled links: list of {"link": str, "role": SourceLinkRole value}.
    links: list[dict[str, str]] = field(default_factory=list)
    # Stable, deterministic identifier (e.g. "ngm:ciaa-press-release:1234").
    document_id: str = ""
    # SourceType hint (one of SourceType values); blank for legacy/untyped docs.
    source_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert manuscript to dictionary for JSON serialization.

        Optional fields (links/document_id/source_type) are omitted when empty
        so legacy url-only manuscripts serialize unchanged.
        """
        result: dict[str, Any] = {
            "url": self.url,
            "file_name": self.file_name,
            "metadata": self.metadata,
        }
        if self.links:
            result["links"] = self.links
        if self.document_id:
            result["document_id"] = self.document_id
        if self.source_type:
            result["source_type"] = self.source_type
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manuscript":
        """Create manuscript from dictionary."""
        return cls(
            url=data["url"],
            file_name=data["file_name"],
            metadata=data.get("metadata", {}),
            links=data.get("links", []),
            document_id=data.get("document_id", ""),
            source_type=data.get("source_type", ""),
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
