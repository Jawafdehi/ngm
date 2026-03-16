"""
NGM Index v2.0 - Tree-Based Hierarchical Index Builder

This module implements the new tree-based index system where:
- Every IndexNode becomes its own JSON file
- $ref stores full HTTPS URLs to child index files
- Only leaf nodes contain manuscripts
- Pagination is handled via 'next' links
"""

import json
import logging
import os
from datetime import datetime
from typing import Any
from cloudpathlib import AnyPath

from .models import Manuscript, IndexNode

logger = logging.getLogger(__name__)

# Configuration
PAGE_SIZE = 300  # Number of manuscripts per page


class IndexBuilder:
    """Builds the hierarchical index tree and writes JSON files."""

    def __init__(
        self, root_path: str, base_url: str, date_str: str, page_size: int = PAGE_SIZE
    ):
        page_size = int(page_size)  # Convert to int
        if page_size <= 0:
            raise ValueError("page_size must be > 0")
        self.root_path = AnyPath(root_path)
        self.base_url = base_url.rstrip("/")
        self.date_str = date_str
        self.indices_base_url = f"{self.base_url}/indices/{date_str}"
        self.page_size = page_size

    def _uploads_path(self, *parts: str):
        """Build path under uploads/ directory, fallback to direct path if uploads/ doesn't exist."""
        uploads_path = self.root_path / "uploads"
        if uploads_path.exists():
            p = uploads_path
            for part in parts:
                p = p / part
            return p
        else:
            # Fallback to direct path for local testing
            p = self.root_path
            for part in parts:
                p = p / part
            return p

    def _relative_path(self, file_path) -> str:
        """Get relative path string from a path object relative to root_path."""
        root_str = str(self.root_path).rstrip("/")
        file_str = str(file_path)
        if file_str.startswith(root_str):
            rel = file_str.replace(root_str, "", 1).lstrip("/")
            return rel
        return file_path.name

    def _build_url(self, file_path) -> str:
        """Construct the full public URL for a file."""
        return f"{self.base_url}/{self._relative_path(file_path)}"

    def build_tree(self) -> IndexNode:
        """Build the complete index tree with full data."""
        root = IndexNode(name="root", path="/")

        # Build each top-level node with full data
        nodes_to_add = []
        total_manuscripts = 0

        for builder_fn in (
            self._build_kanun_patrika_node,
            self._build_ciaa_annual_reports_node,
            self._build_ciaa_press_releases_node,
            # TODO: Add court orders builder once _build_court_orders_node,
            # _build_court_type_node, and _build_court_year_leaf_node are implemented.
        ):
            node = builder_fn()
            if node:
                nodes_to_add.append(node)
                count = self._count_manuscripts_in_tree(node)
                total_manuscripts += count
                logger.info("Indexed %d items for %s", count, node.name)

        root.children = nodes_to_add
        logger.info("Total manuscripts indexed: %d", total_manuscripts)
        return root

    def _build_kanun_patrika_node(self) -> IndexNode | None:
        """Build kanun-patrika node with manuscripts."""
        pdf_dir = self._uploads_path("supreme-court", "kanun-patrika")

        if not pdf_dir.exists():
            return None

        manuscripts = []
        for pdf_path in sorted(pdf_dir.glob("*.pdf"), key=lambda p: p.name):
            manuscripts.append(
                Manuscript(
                    url=self._build_url(pdf_path), file_name=pdf_path.name, metadata={}
                )
            )

        if not manuscripts:
            return None

        # Create leaf node with manuscripts
        return IndexNode(
            name="kanun-patrika", path="/kanun-patrika", manuscripts=manuscripts
        )

    def _build_ciaa_annual_reports_node(self) -> IndexNode | None:
        """Build CIAA annual reports node with manuscripts and metadata."""
        pdf_dir = self._uploads_path("ciaa", "annual-reports", "pdf")
        metadata_dir = self._uploads_path("ciaa", "annual-reports", "metadata")

        if not pdf_dir.exists():
            return None

        manuscripts = []
        for pdf_path in sorted(pdf_dir.glob("*.pdf"), key=lambda p: p.name):
            # Load metadata if available
            metadata = {}
            file_id = pdf_path.stem
            metadata_path = metadata_dir / f"{file_id}.json"

            if metadata_path.exists():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    logger.warning(
                        "Failed to parse JSON metadata for %s: %s", file_id, e
                    )
                except (OSError, UnicodeDecodeError) as e:
                    logger.warning(
                        "Failed to read metadata file for %s: %s", file_id, e
                    )

            manuscripts.append(
                Manuscript(
                    url=self._build_url(pdf_path),
                    file_name=pdf_path.name,
                    metadata=metadata,
                )
            )

        if not manuscripts:
            return None

        # Create leaf node with manuscripts
        return IndexNode(
            name="ciaa-annual-reports",
            path="/ciaa-annual-reports",
            manuscripts=manuscripts,
        )

    def _build_ciaa_press_releases_node(self) -> IndexNode | None:
        """Build CIAA press releases node with manuscripts and metadata."""
        metadata_dir = self._uploads_path("ciaa", "press-releases", "metadata")
        files_dir = self._uploads_path("ciaa", "press-releases", "files")

        if not metadata_dir.exists():
            return None

        manuscripts = []
        for metadata_path in sorted(metadata_dir.glob("*.json"), key=lambda p: p.name):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                logger.warning(
                    "Failed to parse JSON metadata %s: %s", metadata_path.name, e
                )
                continue
            except (OSError, UnicodeDecodeError) as e:
                logger.warning(
                    "Failed to read metadata file %s: %s", metadata_path.name, e
                )
                continue

            # Validation checks
            if not isinstance(metadata, dict):
                logger.warning(
                    "Skipping metadata %s: expected a JSON object", metadata_path.name
                )
                continue

            file_names = metadata.get("file_names", [])
            if not isinstance(file_names, list):
                logger.warning(
                    "Skipping metadata %s: file_names must be a list",
                    metadata_path.name,
                )
                continue

            # Build manuscript entry for each PDF file
            for file_name in file_names:
                pdf_path = files_dir / file_name
                manuscripts.append(
                    Manuscript(
                        url=self._build_url(pdf_path),
                        file_name=file_name,
                        metadata=metadata,
                    )
                )

        if not manuscripts:
            return None

        # Sort by press_id if available
        manuscripts.sort(key=lambda m: m.metadata.get("press_id", 0))

        # Create leaf node with manuscripts
        return IndexNode(
            name="ciaa-press-releases",
            path="/ciaa-press-releases",
            manuscripts=manuscripts,
        )

    def _count_manuscripts_in_tree(self, node: IndexNode) -> int:
        """Recursively count all manuscripts in a tree node."""
        return len(node.manuscripts) + sum(
            self._count_manuscripts_in_tree(child) for child in node.children
        )

    def write_index_files(self, root: IndexNode) -> None:
        """Write all index files to storage."""
        # Create indices directory
        indices_dir = self.root_path / "indices" / self.date_str

        # Clean up existing directory to avoid stale files
        if indices_dir.exists():
            import shutil

            shutil.rmtree(indices_dir)

        indices_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Writing index files...")

        # Single-pass recursive walk - handles any tree depth
        self._write_node(root, indices_dir)

        # Copy root index to index-v2.json at root level
        root_index_path = self.root_path / "index-v2.json"
        root_content = self._node_to_dict_for_file(root)
        root_index_path.write_text(
            json.dumps(root_content, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("Successfully wrote %s", root_index_path)

    def _write_node(self, node: IndexNode, indices_dir) -> None:
        """Single-pass recursive walk — handles any tree depth."""
        if len(node.manuscripts) > self.page_size:
            # Handle pagination
            total_pages = (len(node.manuscripts) + self.page_size - 1) // self.page_size
            logger.info(
                "Applying pagination to %s: %d manuscripts across %d pages",
                node.name,
                len(node.manuscripts),
                total_pages,
            )
            self._write_paginated_node(node, indices_dir, total_pages)
        else:
            # Write single file
            file_path = indices_dir / self._node_filename(node)
            content = self._node_to_dict_for_file(node)
            file_path.write_text(
                json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info("Successfully wrote %s", file_path)

        # Recursively process children - depth doesn't matter
        for child in node.children:
            self._write_node(child, indices_dir)

    def _write_paginated_node(
        self, node: IndexNode, indices_dir, total_pages: int
    ) -> None:
        """Write paginated files for a node with many manuscripts."""
        manuscripts = node.manuscripts
        base_filename = self._node_filename(node).removesuffix(".json")

        for page_num in range(total_pages):
            start_idx = page_num * self.page_size
            page_manuscripts = manuscripts[start_idx : start_idx + self.page_size]

            # Determine filename
            if page_num == 0:
                filename = f"{base_filename}.json"
            else:
                filename = f"{base_filename}.page-{page_num + 1}.json"

            # Create page node
            page_node = IndexNode(
                name=node.name, path=node.path, manuscripts=page_manuscripts
            )

            # Add next link if not the last page
            if page_num < total_pages - 1:
                next_filename = f"{base_filename}.page-{page_num + 2}.json"
                page_node.next_url = f"{self.indices_base_url}/{next_filename}"

            # Write page file
            file_path = indices_dir / filename
            content = self._node_to_dict_for_file(page_node)
            file_path.write_text(
                json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info("Successfully wrote %s", file_path)

    def _node_filename(self, node: IndexNode) -> str:
        """Generate filename for a node."""
        if node.name == "root":
            return "index.json"

        # Convert path to filename: /court-orders/special/081 -> index.court-orders.special.081.json
        path_parts = [part for part in node.path.strip("/").split("/") if part]
        return f"index.{'.'.join(path_parts)}.json"

    def _node_ref_url(self, node: IndexNode) -> str:
        """Generate reference URL for a node."""
        filename = self._node_filename(node)
        return f"{self.indices_base_url}/{filename}"

    def _node_to_dict_for_file(self, node: IndexNode) -> dict[str, Any]:
        """Children serialized as $ref stubs; full data is in each child's own file."""
        result = {"name": node.name, "path": node.path}

        if node.children:
            result["children"] = [
                {"name": c.name, "path": c.path, "$ref": self._node_ref_url(c)}
                for c in node.children
            ]

        if node.manuscripts:
            result["manuscripts"] = [ms.to_dict() for ms in node.manuscripts]

        if node.next_url:
            result["next"] = node.next_url

        return result


def get_base_url() -> str:
    """Get the base URL from environment, defaulting to the production ngm store."""
    return os.getenv("NGM_STORE_BASE_URL", "https://ngm-store.newnepal.org").rstrip("/")


def main() -> None:
    """Build and write the hierarchical index."""
    files_store_env = os.getenv("FILES_STORE")
    if not files_store_env:
        logger.error("FILES_STORE environment variable must be set.")
        raise SystemExit(1)

    files_store = str(files_store_env)
    base_url = get_base_url()
    date_str = datetime.now().strftime("%Y-%m-%d")

    logger.info("Building index v2.0...")
    logger.info("Files store: %s", files_store)
    logger.info("Base URL: %s", base_url)
    logger.info("Date: %s", date_str)

    builder = IndexBuilder(files_store, base_url, date_str)

    # Build tree
    root = builder.build_tree()
    logger.info("Tree built successfully")

    # Validate tree
    try:
        root.validate()
        logger.info("Tree validation passed")
    except ValueError as e:
        logger.error("Tree validation failed: %s", e)
        raise SystemExit(1) from None

    # Write files
    try:
        builder.write_index_files(root)
        logger.info("Index v2.0 build completed successfully")
    except (OSError, TypeError) as e:
        logger.error("Failed to write index files: %s", e)
        raise SystemExit(1) from None


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
