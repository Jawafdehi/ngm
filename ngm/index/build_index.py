"""
NGM Index v2.0 - Tree-Based Hierarchical Index Builder

This module implements the new tree-based index system where:
- Every IndexNode becomes its own JSON file
- $ref stores full HTTPS URLs to child index files
- Only leaf nodes contain manuscripts
- Pagination is handled via 'next' links
"""

import concurrent.futures
import json
import logging
import os
from datetime import datetime
from typing import Any

from cloudpathlib import AnyPath

from .models import Manuscript, IndexNode

logger = logging.getLogger(__name__)

# Configuration
DEFAULT_PAGE_SIZE = 100  # Number of manuscripts per index page
_MAX_WRITE_WORKERS = 10  # Parallel S3 PUT workers


class IndexBuilder:
    """Builds the hierarchical index tree and writes JSON files."""

    def __init__(
        self,
        root_path: str,
        base_url: str,
        date_str: str,
        page_size: int = DEFAULT_PAGE_SIZE,
    ):
        try:
            page_size = int(page_size)  # Convert to int
        except (ValueError, TypeError) as e:
            raise ValueError(f"page_size must be a valid integer: {e}") from e
        if page_size <= 0:
            raise ValueError("page_size must be > 0")
        self.root_path = AnyPath(root_path)
        self.base_url = base_url.rstrip("/")
        self.date_str = date_str
        self.indices_base_url = f"{self.base_url}/indices/{date_str}"
        self.page_size = page_size

    def _build_folder_structure(self, *parts: str):
        """Build path under uploads/ directory."""
        p = self.root_path / "uploads"
        for part in parts:
            p = p / part
        return p

    def _relative_path(self, file_path) -> str:
        """Get relative path string from a path object relative to root_path.

        Raises ValueError if file_path is not under root_path — callers should
        never pass paths outside the configured store root.
        """
        try:
            relative = file_path.relative_to(self.root_path)
            return (
                relative.as_posix()
                if hasattr(relative, "as_posix")
                else str(relative).replace("\\", "/")
            )
        except ValueError as e:
            raise ValueError(
                f"Path '{file_path}' is outside root_path '{self.root_path}'"
            ) from e

    def _build_url(self, file_path) -> str:
        """Construct the full public URL for a file."""
        return f"{self.base_url}/{self._relative_path(file_path)}"

    def build_tree(self) -> IndexNode:
        """Build the complete index tree, running each source builder in parallel."""
        root = IndexNode(name="root", path="/")

        builder_fns = (
            self._build_kanun_patrika_node,
            self._build_ciaa_annual_reports_node,
            self._build_ciaa_press_releases_node,
            # TODO: Add court orders builder once _build_court_orders_node,
            # _build_court_type_node, and _build_court_year_leaf_node are implemented.
        )

        # Each builder scans S3 independently — run them concurrently.
        # ThreadPoolExecutor is fine here: cloudpathlib S3 calls release the GIL.
        results: dict[str, IndexNode | None] = {}

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(builder_fns)
        ) as executor:
            future_to_name = {executor.submit(fn): fn.__name__ for fn in builder_fns}
            for future in concurrent.futures.as_completed(future_to_name):
                fn_name = future_to_name[future]
                try:
                    node = future.result()
                    results[fn_name] = node
                    if node:
                        count = self._count_manuscripts_in_tree(node)
                        logger.info(
                            "%s: indexed %d manuscripts",
                            fn_name,
                            count,
                        )
                    else:
                        logger.info("%s: no data found", fn_name)
                except Exception:
                    logger.exception("%s: builder raised an exception", fn_name)
                    raise

        # Preserve deterministic source ordering regardless of completion order
        # List comprehension already maintains builder_fns order
        nodes_to_add = [
            results[fn.__name__]
            for fn in builder_fns
            if results.get(fn.__name__) is not None
        ]

        root.children = nodes_to_add
        total = sum(self._count_manuscripts_in_tree(n) for n in nodes_to_add)
        logger.info(
            "Tree built — %d total manuscripts across %d sources",
            total,
            len(nodes_to_add),
        )
        return root

    def _build_kanun_patrika_node(self) -> IndexNode | None:
        """Build kanun-patrika node with manuscripts."""
        pdf_dir = self._build_folder_structure("supreme-court", "kanun-patrika")

        if not pdf_dir.exists():
            return None

        manuscripts = []
        for pdf_path in sorted(pdf_dir.glob("*.pdf"), key=lambda p: p.name):
            manuscripts.append(
                Manuscript(
                    url=self._build_url(pdf_path), file_name=pdf_path.name, metadata={}
                )
            )
            logger.debug("kanun-patrika: found %s", pdf_path.name)

        if not manuscripts:
            return None

        logger.info("kanun-patrika: %d PDFs scanned", len(manuscripts))
        return IndexNode(
            name="kanun-patrika", path="/kanun-patrika", manuscripts=manuscripts
        )

    def _build_ciaa_annual_reports_node(self) -> IndexNode | None:
        """Build CIAA annual reports node with manuscripts and metadata."""
        pdf_dir = self._build_folder_structure("ciaa", "annual-reports", "pdf")
        metadata_dir = self._build_folder_structure(
            "ciaa", "annual-reports", "metadata"
        )

        if not pdf_dir.exists():
            return None

        manuscripts = []
        for pdf_path in sorted(pdf_dir.glob("*.pdf"), key=lambda p: p.name):
            file_id = pdf_path.stem
            metadata_path = metadata_dir / f"{file_id}.json"

            try:
                raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            except FileNotFoundError as e:
                # Metadata is mandatory for annual reports — CiaaAnnualReportsPipeline
                # always writes a metadata JSON alongside every PDF download.
                # A missing metadata file means the scraper pipeline broke mid-run,
                # not a timing/lag issue like press releases. Throw to fail loudly.
                raise FileNotFoundError(
                    f"ciaa-annual-reports: metadata missing for {file_id}"
                    f" — scraper may not have written it"
                ) from e
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"ciaa-annual-reports: malformed JSON in metadata {file_id}: {e}"
                ) from e
            except (OSError, UnicodeDecodeError) as e:
                raise RuntimeError(
                    f"ciaa-annual-reports: failed to read metadata {file_id}: {e}"
                ) from e

            if not isinstance(raw, dict):
                raise ValueError(
                    f"ciaa-annual-reports: metadata {file_id} is not a JSON object"
                    f" — got {type(raw).__name__}"
                )

            manuscripts.append(
                Manuscript(
                    url=self._build_url(pdf_path),
                    file_name=pdf_path.name,
                    metadata=raw,
                )
            )
            logger.debug("ciaa-annual-reports: loaded %s", file_id)

        if not manuscripts:
            return None

        # Create leaf node with manuscripts
        logger.info("ciaa-annual-reports: %d PDFs scanned", len(manuscripts))
        return IndexNode(
            name="ciaa-annual-reports",
            path="/ciaa-annual-reports",
            manuscripts=manuscripts,
        )

    def _build_ciaa_press_releases_node(self) -> IndexNode | None:
        """Build CIAA press releases node with manuscripts and metadata."""
        metadata_dir = self._build_folder_structure(
            "ciaa", "press-releases", "metadata"
        )
        files_dir = self._build_folder_structure("ciaa", "press-releases", "files")

        if not metadata_dir.exists():
            return None

        manuscripts = []
        releases_attempted = 0

        for metadata_path in sorted(metadata_dir.glob("*.json"), key=lambda p: p.name):
            releases_attempted += 1

            # Metadata file itself failing → throw.
            # File-based checkpointing means only new files appear each run,
            # so a malformed new metadata file means the scraper pipeline broke.
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except FileNotFoundError as e:
                raise FileNotFoundError(
                    f"ciaa-press-releases: metadata file disappeared mid-scan:"
                    f" {metadata_path.name}"
                ) from e
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"ciaa-press-releases: malformed JSON in {metadata_path.name}: {e}"
                ) from e
            except (OSError, UnicodeDecodeError) as e:
                raise RuntimeError(
                    f"ciaa-press-releases: failed to read {metadata_path.name}: {e}"
                ) from e

            if not isinstance(metadata, dict):
                raise ValueError(
                    f"ciaa-press-releases: {metadata_path.name} is not a JSON object"
                    f" — got {type(metadata).__name__}"
                )

            if "file_names" not in metadata:
                raise ValueError(
                    f"ciaa-press-releases: {metadata_path.name} missing file_names"
                )
            file_names = metadata["file_names"]
            if not isinstance(file_names, list):
                raise ValueError(
                    f"ciaa-press-releases: {metadata_path.name}"
                    f" file_names must be a list"
                )

            # Build manuscript entry for each PDF file
            press_id = metadata.get("press_id", "?")
            files_added = 0
            for file_name in file_names:
                # Validate file_name is a non-empty string
                if not isinstance(file_name, str) or not file_name.strip():
                    raise ValueError(
                        f"ciaa-press-releases: invalid file_name"
                        f" in press_id={press_id}: {file_name!r}"
                    )

                # PDF not existing in R2 → warn only, do not throw.
                # Metadata is the source of truth for press releases.
                # The scraper writes metadata after the file download completes,
                # but R2 upload and propagation are not atomic — the PDF may lag
                # behind the metadata write. The URL is still valid to index;
                # the user gets a 404 at download time, which is preferable
                # to blocking the entire index build over one lagging file.
                pdf_path = files_dir / file_name
                manuscripts.append(
                    Manuscript(
                        url=self._build_url(pdf_path),
                        file_name=file_name,
                        metadata=metadata,
                    )
                )
                files_added += 1

            logger.debug(
                "ciaa-press-releases: press_id=%s — %d file(s) added",
                press_id,
                files_added,
            )

        if not manuscripts:
            return None

        # Sort by press_id if available
        manuscripts.sort(key=lambda m: m.metadata.get("press_id", 0), reverse=True)

        # Create leaf node with manuscripts
        logger.info(
            "ciaa-press-releases: %d manuscripts from %d release(s)",
            len(manuscripts),
            releases_attempted,
        )
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
        """Write all index files to storage using parallel S3 PUTs.

        Collects all (path, content) pairs first (no I/O), then fires all
        writes concurrently via a thread pool.
        """
        indices_dir = self.root_path / "indices" / self.date_str
        indices_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Collecting write jobs...")
        pending: list[tuple] = []
        self._collect_write_jobs(root, indices_dir, pending)
        logger.info("  %d files to write (page_size=%d)", len(pending), self.page_size)

        logger.info("Writing index files (parallel, %d workers)...", _MAX_WRITE_WORKERS)
        failed: list[str] = []

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=_MAX_WRITE_WORKERS
        ) as executor:
            future_to_path = {
                executor.submit(self._write_file, path, content): path
                for path, content in pending
            }
            done = 0
            for future in concurrent.futures.as_completed(future_to_path):
                path = future_to_path[future]
                done += 1
                try:
                    future.result()
                    logger.debug("[%d/%d] wrote %s", done, len(pending), path)
                except (OSError, TypeError) as e:
                    logger.error("Failed to write %s: %s", path, e)
                    failed.append(str(path))

        if failed:
            logger.error(
                "Write phase completed with %d failure(s): %s",
                len(failed),
                failed,
            )
            raise RuntimeError(f"{len(failed)} index file(s) failed to write: {failed}")

        logger.info("Wrote %d index files", len(pending))

        # Root-level alias — same content, one extra PUT
        root_index_path = self.root_path / "index-v2.json"
        root_content = self._node_to_dict_for_file(root)
        root_index_path.write_text(
            json.dumps(root_content, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("Wrote root alias %s", root_index_path)

    def _write_file(self, path, content: dict) -> None:
        """Write a single JSON file — called from thread pool."""
        path.write_text(
            json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _collect_write_jobs(self, node: IndexNode, indices_dir, pending: list) -> None:
        """Walk the tree and collect all (path, content) pairs without any I/O."""
        if len(node.manuscripts) > self.page_size:
            total_pages = (len(node.manuscripts) + self.page_size - 1) // self.page_size
            logger.info(
                "  pagination: %s → %d manuscripts across %d pages",
                node.name,
                len(node.manuscripts),
                total_pages,
            )
            self._collect_paginated_jobs(node, indices_dir, total_pages, pending)
        else:
            file_path = indices_dir / self._node_filename(node)
            content = self._node_to_dict_for_file(node)
            pending.append((file_path, content))

        # Recursively process children - depth doesn't matter
        for child in node.children:
            self._collect_write_jobs(child, indices_dir, pending)

    def _collect_paginated_jobs(
        self, node: IndexNode, indices_dir, total_pages: int, pending: list
    ) -> None:
        """Collect paginated write jobs without any I/O."""
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

            # Add next link if not the last page, otherwise explicitly set to None
            if page_num < total_pages - 1:
                next_filename = f"{base_filename}.page-{page_num + 2}.json"
                page_node.next_url = f"{self.indices_base_url}/{next_filename}"
            else:
                page_node.next_url = None

            # Write page file
            file_path = indices_dir / filename
            content = self._node_to_dict_for_file(page_node)
            pending.append((file_path, content))

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
        result: dict[str, Any] = {"name": node.name, "path": node.path}

        if node.children:
            result["children"] = [
                {"name": c.name, "path": c.path, "$ref": self._node_ref_url(c)}
                for c in node.children
            ]

        if node.manuscripts:
            result["manuscripts"] = [ms.to_dict() for ms in node.manuscripts]

        if node.next_url is not None:
            result["next"] = node.next_url
        elif hasattr(node, "next_url"):
            # Explicitly include "next": null for paginated nodes
            result["next"] = None

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

    logger.info("Building index ....")
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
        logger.info("Index build completed successfully")
    except (OSError, TypeError, RuntimeError) as e:
        logger.error("Failed to write index files: %s", e)
        raise SystemExit(1) from None


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    main()
