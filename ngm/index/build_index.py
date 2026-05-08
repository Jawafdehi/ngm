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
from sqlalchemy.exc import SQLAlchemyError

from .models import Manuscript, IndexNode
from ..database.models import get_engine, get_session, CourtCase

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

        # Database engine for fiscal year validation (lazy initialized)
        # Sessions are created per-thread to avoid threading issues
        self._engine = None

    @property
    def engine(self):
        """Lazy-load database engine only when needed."""
        if self._engine is None:
            self._engine = get_engine()
        return self._engine

    def _build_folder_structure(self, *parts: str):
        """Build path under uploads/ directory."""
        p = self.root_path / "uploads"
        for part in parts:
            p = p / part
        return p

    def _calculate_fiscal_year_from_registration(
        self, registration_bs: str
    ) -> int | None:
        """Calculate fiscal year from BS registration date.

        Nepal's fiscal year runs from Shrawan (month 4) to Ashar (month 3).
        Months 1-3 belong to previous fiscal year, months 4-12 to current year.
        """
        try:
            parts = registration_bs.split("-")
            if len(parts) != 3:
                return None

            year = int(parts[0])
            month = int(parts[1])

            if 1 <= month <= 3:
                # Baisakh, Jestha, Ashar - belongs to previous fiscal year
                return year - 1
            elif 4 <= month <= 12:
                # Shrawan through Chaitra - belongs to current fiscal year
                return year
            return None
        except (ValueError, IndexError):
            return None

    def _extract_case_number_from_filename(self, filename: str) -> str:
        """Extract case number from filename by removing extension and version numbers."""
        # Remove file extension: "082-OA-0503.1.docx" -> "082-OA-0503.1"
        name_without_ext = filename.rsplit(".", 1)[0] if "." in filename else filename
        # Remove trailing version numbers: "082-OA-0503.1" -> "082-OA-0503"
        case_number = (
            name_without_ext.rsplit(".", 1)[0]
            if "." in name_without_ext
            else name_without_ext
        )
        return case_number

    def _lookup_case_fiscal_year(
        self, case_number: str, court_identifier: str
    ) -> tuple[str, str]:
        """Look up case in database and return fiscal year and registration date."""
        # Create a new session for this thread
        session = get_session(self.engine)

        try:
            with session.begin():
                case = (
                    session.query(CourtCase)
                    .filter(
                        CourtCase.case_number == case_number,
                        CourtCase.court_identifier == court_identifier,
                    )
                    .first()
                )

                if not case:
                    raise ValueError(
                        f"Case {case_number} not found in database for court {court_identifier}. "
                        f"Cannot determine fiscal year for indexing."
                    )

                if not case.registration_date_bs:
                    raise ValueError(
                        f"Case {case_number} found in database but registration_date_bs is missing. "
                        f"Cannot determine fiscal year for indexing."
                    )

                fiscal_year = self._calculate_fiscal_year_from_registration(
                    case.registration_date_bs
                )
                if fiscal_year is None:
                    raise ValueError(
                        f"Case {case_number} has invalid registration_date_bs format: {case.registration_date_bs}. "
                        f"Cannot determine fiscal year for indexing."
                    )

                # Convert to 3-digit year string (e.g., 2067 -> "067")
                year_str = str(fiscal_year)
                if len(year_str) == 4 and year_str.startswith("20"):
                    fiscal_year_3digit = year_str[2:].zfill(
                        3
                    )  # "2067" -> "067", "2063" -> "063"
                    return (fiscal_year_3digit, case.registration_date_bs)
                else:
                    raise ValueError(
                        f"Case {case_number} has unexpected fiscal year format: {fiscal_year}"
                    )
        finally:
            session.close()

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
            self._build_court_orders_node,
            self._build_ppmo_blacklist_node,
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

    def _process_annual_report_pdf(self, pdf_path, metadata_dir) -> Manuscript:
        """Process a single annual report PDF and its metadata."""
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

        return Manuscript(
            url=self._build_url(pdf_path),
            file_name=pdf_path.name,
            metadata=raw,
        )

    def _build_ciaa_annual_reports_node(self) -> IndexNode | None:
        """Build CIAA annual reports node with manuscripts and metadata."""
        pdf_dir = self._build_folder_structure("ciaa", "annual-reports", "pdf")
        metadata_dir = self._build_folder_structure(
            "ciaa", "annual-reports", "metadata"
        )

        if not pdf_dir.exists():
            return None

        pdf_paths = sorted(pdf_dir.glob("*.pdf"), key=lambda p: p.name)

        logger.info(
            "ciaa-annual-reports: processing %d PDFs in parallel...", len(pdf_paths)
        )

        manuscripts = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            future_to_path = {
                executor.submit(
                    self._process_annual_report_pdf, path, metadata_dir
                ): path
                for path in pdf_paths
            }

            for future in concurrent.futures.as_completed(future_to_path):
                path = future_to_path[future]
                try:
                    manuscript = future.result()
                    manuscripts.append(manuscript)
                except Exception:
                    logger.exception(
                        "ciaa-annual-reports: failed to process %s", path.name
                    )
                    raise

        if not manuscripts:
            return None

        # Sort by file_name for deterministic ordering
        manuscripts.sort(key=lambda m: m.file_name)

        logger.info("ciaa-annual-reports: %d PDFs scanned", len(manuscripts))
        # Create leaf node with manuscripts
        return IndexNode(
            name="ciaa-annual-reports",
            path="/ciaa-annual-reports",
            manuscripts=manuscripts,
        )

    def _process_press_release_metadata(
        self, metadata_path, files_dir
    ) -> list[Manuscript]:
        """Process a single press release metadata file and return manuscripts."""
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
        manuscripts = []
        for file_name in file_names:
            # Validate file_name is a non-empty string
            if not isinstance(file_name, str) or not file_name.strip():
                raise ValueError(
                    f"ciaa-press-releases: invalid file_name"
                    f" in {metadata_path.name}: {file_name!r}"
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

        return manuscripts

    def _build_ciaa_press_releases_node(self) -> IndexNode | None:
        """Build CIAA press releases node with manuscripts and metadata."""
        metadata_dir = self._build_folder_structure(
            "ciaa", "press-releases", "metadata"
        )
        files_dir = self._build_folder_structure("ciaa", "press-releases", "files")

        if not metadata_dir.exists():
            return None

        metadata_paths = sorted(metadata_dir.glob("*.json"), key=lambda p: p.name)

        logger.info(
            "ciaa-press-releases: processing %d metadata files in parallel...",
            len(metadata_paths),
        )

        all_manuscripts = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            future_to_path = {
                executor.submit(
                    self._process_press_release_metadata, path, files_dir
                ): path
                for path in metadata_paths
            }

            completed = 0
            for future in concurrent.futures.as_completed(future_to_path):
                path = future_to_path[future]

                try:
                    manuscripts = future.result()
                    all_manuscripts.extend(manuscripts)
                    completed += 1
                    if completed % 100 == 0:
                        logger.info(
                            "ciaa-press-releases: processed %d/%d files",
                            completed,
                            len(metadata_paths),
                        )
                except Exception:
                    logger.exception(
                        "ciaa-press-releases: failed to process %s", path.name
                    )
                    raise

        if not all_manuscripts:
            return None

        # Sort by press_id if available
        all_manuscripts.sort(key=lambda m: m.metadata.get("press_id", 0), reverse=True)

        # Create leaf node with manuscripts
        logger.info(
            "ciaa-press-releases: %d manuscripts from %d release(s)",
            len(all_manuscripts),
            len(metadata_paths),
        )
        return IndexNode(
            name="ciaa-press-releases",
            path="/ciaa-press-releases",
            manuscripts=all_manuscripts,
        )

    def _process_ppmo_blacklist_metadata(self, metadata_path) -> Manuscript:
        """Process a single PPMO blacklist metadata file and return a manuscript."""
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(
                f"ppmo-blacklist: malformed JSON in {metadata_path.name}: {e}"
            ) from e

        if not isinstance(metadata, dict):
            raise ValueError(
                f"ppmo-blacklist: {metadata_path.name} is not a JSON object"
            )

        # PPMO currently doesn't have a PDF, so we use a placeholder or just the metadata
        # For now, we'll use a placeholder URL or the source URL if available
        return Manuscript(
            url=metadata.get("source_url", ""),
            file_name=metadata_path.name,
            metadata=metadata,
        )

    def _build_ppmo_blacklist_node(self) -> IndexNode | None:
        """Build PPMO blacklist node with metadata."""
        metadata_dir = self._build_folder_structure("ppmo", "blacklist", "metadata")

        if not metadata_dir.exists():
            return None

        metadata_paths = sorted(metadata_dir.glob("*.json"), key=lambda p: p.name)

        if not metadata_paths:
            return None

        logger.info(
            "ppmo-blacklist: processing %d metadata files...", len(metadata_paths)
        )

        manuscripts = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            future_to_path = {
                executor.submit(self._process_ppmo_blacklist_metadata, path): path
                for path in metadata_paths
            }

            for future in concurrent.futures.as_completed(future_to_path):
                path = future_to_path[future]
                try:
                    ms = future.result()
                    manuscripts.append(ms)
                except Exception:
                    logger.exception("ppmo-blacklist: failed to process %s", path.name)
                    raise

        # Sort by date (filename starts with date)
        manuscripts.sort(key=lambda m: m.file_name, reverse=True)

        return IndexNode(
            name="ppmo-blacklist",
            path="/ppmo-blacklist",
            manuscripts=manuscripts,
        )

    def _build_court_orders_node(self) -> IndexNode | None:
        """Build court-orders branch node with supreme and special children."""
        court_orders_dir = self._build_folder_structure("court-orders")

        if not court_orders_dir.exists():
            return None

        # Build children for each court type
        children = []
        for court_path in sorted(court_orders_dir.iterdir()):
            if not court_path.is_dir():
                continue

            court_identifier = court_path.name
            court_node = self._build_court_type_node(court_identifier)
            if court_node:
                children.append(court_node)

        if not children:
            return None

        # Sort children by court type for deterministic ordering
        children.sort(key=lambda n: n.name)

        logger.info("court-orders: built with %d court type(s)", len(children))
        return IndexNode(name="court-orders", path="/court-orders", children=children)

    def _build_court_type_node(self, court_type: str) -> IndexNode | None:
        """Build branch node for a specific court type (supreme or special)."""
        court_dir = self._build_folder_structure("court-orders", court_type)

        if not court_dir.exists():
            return None

        # Group files by year (extracted from filename pattern: YYY-...)
        # Log progress every log_interval files for visibility on large directories
        year_groups: dict[str, list] = {}
        log_interval = 10000  # Log progress every 10k files
        file_count = 0

        for file_path in court_dir.iterdir():
            if not file_path.is_file():
                continue

            file_count += 1

            filename = file_path.name
            case_number = self._extract_case_number_from_filename(filename)

            # Check if filename matches expected pattern: YYY-TYPE-NNNN
            # First part should be a 3-digit year
            parts = filename.split("-")
            filename_year = None

            if len(parts) >= 2:
                first_part = parts[0]
                if first_part.isdigit() and len(first_part) == 3:
                    filename_year = first_part

            # Determine year: use filename if available, otherwise lookup in database
            if filename_year is not None:
                year = filename_year
            else:
                # Unexpected pattern - lookup in database
                try:
                    year, registration_date = self._lookup_case_fiscal_year(
                        case_number, court_type
                    )
                    logger.debug(
                        "court-orders/%s: Case %s (filename: %s) → FY %s (registration: %s)",
                        court_type,
                        case_number,
                        filename,
                        year,
                        registration_date,
                    )
                except (ValueError, SQLAlchemyError) as e:
                    logger.error(
                        "court-orders/%s: Failed to determine fiscal year for case %s (filename: %s): %s",
                        court_type,
                        case_number,
                        filename,
                        e,
                    )
                    raise

            if year not in year_groups:
                year_groups[year] = []
            year_groups[year].append(file_path)

            # Log progress when interval reached
            if file_count % log_interval == 0:
                logger.info(
                    "court-orders/%s: processed %d files so far...",
                    court_type,
                    file_count,
                )

        if not year_groups:
            return None

        logger.info(
            "court-orders/%s: processing %d year(s) in parallel...",
            court_type,
            len(year_groups),
        )

        # Create child nodes for each year in parallel
        children = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(
                    self._build_court_year_branch_node,
                    court_type,
                    year,
                    year_groups[year],
                ): (
                    court_type,
                    year,
                )  # Store context as tuple
                for year in sorted(year_groups.keys())
            }

            for future in concurrent.futures.as_completed(futures):
                court_type_ctx, year_ctx = futures[future]
                try:
                    year_node = future.result()
                    if year_node:
                        children.append(year_node)
                except Exception:
                    logger.exception(
                        "court-orders/%s: failed to process year %s",
                        court_type_ctx,
                        year_ctx,
                    )
                    raise

        if not children:
            return None

        # Sort children by year for deterministic ordering
        children.sort(key=lambda n: n.name)

        logger.info("court-orders/%s: built with %d year(s)", court_type, len(children))
        return IndexNode(
            name=court_type,
            path=f"/court-orders/{court_type}",
            children=children,
        )

    def _build_court_year_branch_node(
        self, court_type: str, year: str, file_paths: list
    ) -> IndexNode | None:
        """Build branch node for a specific year, grouping by case number."""
        # Group files by case number (e.g., "082-OA-0503.1.docx" -> "082-OA-0503")
        case_groups: dict[str, list] = {}

        for file_path in file_paths:
            # Extract case number using helper method
            case_number = self._extract_case_number_from_filename(file_path.name)

            if case_number not in case_groups:
                case_groups[case_number] = []
            case_groups[case_number].append(file_path)

        if not case_groups:
            return None

        # Only log for large years to avoid spam
        if len(case_groups) > 1000:
            logger.info(
                "court-orders/%s/%s: processing %d case(s) in parallel...",
                court_type,
                year,
                len(case_groups),
            )

        # Create child nodes for each case number in parallel
        children = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(
                    self._build_court_case_leaf_node,
                    court_type,
                    year,
                    case_number,
                    case_groups[case_number],
                ): (
                    court_type,
                    year,
                    case_number,
                )  # Store context as tuple
                for case_number in sorted(case_groups.keys())
            }

            for future in concurrent.futures.as_completed(futures):
                court_type_ctx, year_ctx, case_number_ctx = futures[future]
                try:
                    case_node = future.result()
                    if case_node:
                        children.append(case_node)
                except Exception:
                    logger.exception(
                        "court-orders/%s/%s: failed to process case %s",
                        court_type_ctx,
                        year_ctx,
                        case_number_ctx,
                    )
                    raise

        if not children:
            return None

        # Sort children by case number for deterministic ordering
        children.sort(key=lambda n: n.name)

        # Summary log instead of per-case logging
        logger.info(
            "court-orders/%s/%s: built %d case(s)",
            court_type,
            year,
            len(children),
        )
        return IndexNode(
            name=year,
            path=f"/court-orders/{court_type}/{year}",
            children=children,
        )

    def _build_court_case_leaf_node(
        self, court_type: str, year: str, case_number: str, file_paths: list
    ) -> IndexNode | None:
        """Build leaf node for a specific case with manuscripts."""
        manuscripts = []

        for file_path in file_paths:
            manuscripts.append(
                Manuscript(
                    url=self._build_url(file_path),
                    file_name=file_path.name,
                    metadata={},
                )
            )
            # Debug logging removed to avoid 1.5M+ log lines

        if not manuscripts:
            return None

        # No per-case logging - summary logged at year level
        return IndexNode(
            name=case_number,
            path=f"/court-orders/{court_type}/{year}/{case_number}",
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
        # Check if node needs pagination (either manuscripts or children)
        if len(node.manuscripts) > self.page_size:
            total_pages = (len(node.manuscripts) + self.page_size - 1) // self.page_size
            logger.info(
                "  pagination: %s → %d manuscripts across %d pages",
                node.name,
                len(node.manuscripts),
                total_pages,
            )
            self._collect_paginated_manuscripts_jobs(
                node, indices_dir, total_pages, pending
            )
        elif len(node.children) > self.page_size:
            total_pages = (len(node.children) + self.page_size - 1) // self.page_size
            logger.info(
                "  pagination: %s → %d children across %d pages",
                node.name,
                len(node.children),
                total_pages,
            )
            self._collect_paginated_children_jobs(
                node, indices_dir, total_pages, pending
            )
        else:
            file_path = indices_dir / self._node_filename(node)
            content = self._node_to_dict_for_file(node)
            pending.append((file_path, content))

        # Recursively process children - depth doesn't matter
        for child in node.children:
            self._collect_write_jobs(child, indices_dir, pending)

    def _collect_paginated_manuscripts_jobs(
        self, node: IndexNode, indices_dir, total_pages: int, pending: list
    ) -> None:
        """Collect paginated write jobs for nodes with many manuscripts."""
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

    def _collect_paginated_children_jobs(
        self, node: IndexNode, indices_dir, total_pages: int, pending: list
    ) -> None:
        """Collect paginated write jobs for nodes with many children."""
        children = node.children
        base_filename = self._node_filename(node).removesuffix(".json")

        for page_num in range(total_pages):
            start_idx = page_num * self.page_size
            page_children = children[start_idx : start_idx + self.page_size]

            # Determine filename
            if page_num == 0:
                filename = f"{base_filename}.json"
            else:
                filename = f"{base_filename}.page-{page_num + 1}.json"

            # Create page node
            page_node = IndexNode(
                name=node.name, path=node.path, children=page_children
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
    return os.getenv("NGM_STORE_BASE_URL", "https://ngm-store.jawafdehi.org").rstrip(
        "/"
    )


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
