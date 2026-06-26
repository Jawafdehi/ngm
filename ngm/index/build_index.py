"""
NGM Index v2.0 - Tree-Based Hierarchical Index Builder

This module implements the new tree-based index system where:
- Every IndexNode becomes its own JSON file
- $ref stores full HTTPS URLs to child index files
- Only leaf nodes contain manuscripts
- Pagination is handled via 'next' links
"""

import concurrent.futures
import hashlib
import html
import json
import logging
import os
import re
import shutil
import tempfile
from collections import deque
from datetime import datetime
from typing import Any

from cloudpathlib import AnyPath
from sqlalchemy.exc import SQLAlchemyError

from .models import (
    Manuscript,
    IndexNode,
    SourceLinkRole,
    SourceType,
    document_html_relpath,
)
from ..database.models import get_engine, get_session, CourtCase

logger = logging.getLogger(__name__)

# Configuration
DEFAULT_PAGE_SIZE = 100  # Number of manuscripts per index page
_MAX_WRITE_WORKERS = 10  # Parallel S3 PUT workers
# A single sitemap file may list at most 50,000 URLs (sitemaps.org limit).
SITEMAP_MAX_URLS = 50000
# Max HTML-page writes in flight at once during the streaming SEO pass. Bounds
# peak memory (rendered HTML held until written) regardless of dataset size, so
# emitting 1M+ landing pages does not materialize 1M strings at once.
_SEO_MAX_INFLIGHT = 256


def _slugify(text: str) -> str:
    """Make a URL/key-safe slug, preserving ASCII alphanumerics and Devanagari.

    Used to derive stable document ids and HTML page paths from filenames. Keeps
    Nepali (Devanagari, U+0900–U+097F) so ids stay human-meaningful.
    """
    text = (text or "").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^0-9a-zऀ-ॿ._-]", "", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "doc"


def _slug_with_hash(text: str) -> str:
    """A readable slug with a short deterministic hash suffix.

    Used for document ids derived from filenames (kanun-patrika, annual reports,
    PPMO). The hash makes the id collision-free even when two distinct filenames
    slugify to the same value (e.g. ``Report (2080)`` and ``Report 2080``), while
    keeping the slug for human readability.
    """
    digest = hashlib.blake2s(text.encode("utf-8"), digest_size=4).hexdigest()
    return f"{_slugify(text)}-{digest}"


class IndexBuilder:
    """Builds the hierarchical index tree and writes JSON files."""

    def __init__(
        self,
        root_path: str,
        base_url: str,
        date_str: str,
        page_size: int = DEFAULT_PAGE_SIZE,
        output_path: str | None = None,
    ):
        try:
            page_size = int(page_size)  # Convert to int
        except (ValueError, TypeError) as e:
            raise ValueError(f"page_size must be a valid integer: {e}") from e
        if page_size <= 0:
            raise ValueError("page_size must be > 0")
        # root_path is the READ source (scraper uploads/ + DB-derived inputs).
        # output_root is where derived files are WRITTEN — a local staging dir
        # in production so the build runs against fast local disk and uploads to
        # R2 once at the end. Defaults to root_path (local/test: read==write).
        self.root_path = AnyPath(root_path)
        self.output_root = AnyPath(output_path) if output_path else self.root_path
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

    @staticmethod
    def _file_links(file_urls: list[str]) -> list[dict[str, str]]:
        """Turn file URLs into a roled link list mirroring DocumentSource.url.

        The primary file (PDF preferred) gets role RAW; every other file is an
        ALTERNATE rendering. Order is otherwise preserved (stable sort).
        """
        ordered = sorted(
            [u for u in file_urls if u],
            key=lambda u: 0 if u.lower().endswith(".pdf") else 1,
        )
        links: list[dict[str, str]] = []
        for i, url in enumerate(ordered):
            role = SourceLinkRole.RAW if i == 0 else SourceLinkRole.ALTERNATE
            links.append({"link": url, "role": role.value})
        return links

    @staticmethod
    def _append_source_page(
        links: list[dict[str, str]], source_url: str | None
    ) -> None:
        """Append a SOURCE_PAGE link (the gov page a document was published on)."""
        if source_url and source_url.strip():
            links.append(
                {"link": source_url.strip(), "role": SourceLinkRole.SOURCE_PAGE.value}
            )

    @staticmethod
    def _primary_url(links: list[dict[str, str]]) -> str:
        """The back-compat ``url`` value: first link's target (RAW if present)."""
        return links[0]["link"] if links else ""

    @staticmethod
    def _document_html_relpath(document_id: str) -> str:
        """Map a document_id to its static HTML landing-page key under /d/.

        Thin wrapper over ``models.document_html_relpath`` (the single source of
        truth, also used by the DB indexer).
        """
        return document_html_relpath(document_id)

    def _document_url(self, document_id: str) -> str:
        """Full public URL of a document's HTML landing page."""
        return f"{self.base_url}/{self._document_html_relpath(document_id)}"

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
            url = self._build_url(pdf_path)
            links = self._file_links([url])
            manuscripts.append(
                Manuscript(
                    url=url,
                    file_name=pdf_path.name,
                    metadata={},
                    links=links,
                    document_id=f"ngm:kanun-patrika:{_slug_with_hash(pdf_path.stem)}",
                    source_type=SourceType.MISC.value,
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

        url = self._build_url(pdf_path)
        links = self._file_links([url])
        self._append_source_page(links, raw.get("source_url"))
        return Manuscript(
            url=url,
            file_name=pdf_path.name,
            metadata=raw,
            links=links,
            document_id=f"ngm:ciaa-annual-report:{_slug_with_hash(file_id)}",
            source_type=SourceType.MISC.value,
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
    ) -> Manuscript | None:
        """Process a press release metadata file into ONE logical-document manuscript.

        A press release may ship several attachments (PDF + DOC); they collapse
        into a single manuscript whose ``links`` carry the primary file as RAW,
        the rest as ALTERNATE, and the CIAA page as SOURCE_PAGE — exactly the
        DocumentSource shape. Returns None only when there is nothing to index.
        """
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

        # Build one roled link per attachment. PDF (if any) becomes RAW, the
        # rest ALTERNATE; the CIAA landing page is SOURCE_PAGE.
        #
        # A missing file in R2 → still indexed (warn-not-throw): metadata is the
        # source of truth and R2 upload can lag the metadata write; a 404 at
        # download time beats blocking the whole build over one lagging file.
        file_urls = []
        for file_name in file_names:
            # Validate file_name is a non-empty string
            if not isinstance(file_name, str) or not file_name.strip():
                raise ValueError(
                    f"ciaa-press-releases: invalid file_name"
                    f" in {metadata_path.name}: {file_name!r}"
                )
            file_urls.append(self._build_url(files_dir / file_name))

        links = self._file_links(file_urls)
        self._append_source_page(links, metadata.get("source_url"))
        if not links:
            # No attachments and no source page — nothing addressable to index.
            return None

        # press_id is the stable key; fall back to the metadata filename stem
        # (files are named "{press_id}.json") so the document_id is never
        # "...:None" when the field is absent from the metadata body.
        press_id = metadata.get("press_id")
        if press_id is None:
            press_id = metadata_path.stem
        primary_name = file_names[0] if file_names else metadata_path.stem
        return Manuscript(
            url=self._primary_url(links),
            file_name=primary_name,
            metadata=metadata,
            links=links,
            document_id=f"ngm:ciaa-press-release:{press_id}",
            source_type=SourceType.CIAA_PRESS_RELEASE.value,
        )

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
                    manuscript = future.result()
                    if manuscript is not None:
                        all_manuscripts.append(manuscript)
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

        # Sort by press_id if available (one manuscript == one press release)
        all_manuscripts.sort(key=lambda m: m.metadata.get("press_id", 0), reverse=True)

        # Create leaf node with manuscripts
        logger.info(
            "ciaa-press-releases: %d release(s) indexed",
            len(all_manuscripts),
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

        # PPMO has no document file — the record itself is the content. Its only
        # link is the SOURCE_PAGE; the structured metadata is rendered on the
        # generated HTML landing page.
        source_url = metadata.get("source_url", "")
        links: list[dict[str, str]] = []
        self._append_source_page(links, source_url)
        return Manuscript(
            url=source_url,
            file_name=metadata_path.name,
            metadata=metadata,
            links=links,
            document_id=f"ngm:ppmo-blacklist:{_slug_with_hash(metadata_path.stem)}",
            source_type=SourceType.MISC.value,
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
        """Build leaf node for a case as ONE logical-document manuscript.

        All order files for a case (multiple versions / formats) collapse into a
        single manuscript: the primary order is RAW, the rest ALTERNATE — the
        DocumentSource shape, one source per case.
        """
        file_urls = [self._build_url(fp) for fp in file_paths]
        links = self._file_links(file_urls)
        if not links:
            return None

        primary_url = self._primary_url(links)
        manuscript = Manuscript(
            url=primary_url,
            file_name=primary_url.rsplit("/", 1)[-1],
            metadata={},
            links=links,
            document_id=f"ngm:court-order:{court_type}:{case_number}",
            source_type=SourceType.COURT_ORDER.value,
        )

        # No per-case logging - summary logged at year level
        return IndexNode(
            name=case_number,
            path=f"/court-orders/{court_type}/{year}/{case_number}",
            manuscripts=[manuscript],
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
        indices_dir = self.output_root / "indices" / self.date_str
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
        root_index_path = self.output_root / "index-v2.json"
        root_content = self._node_to_dict_for_file(root)
        root_index_path.write_text(
            json.dumps(root_content, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("Wrote root alias %s", root_index_path)

        # SEO surface: crawlable HTML landing pages + sitemap index + robots.txt.
        # The JSON index above stays the programmatic API; these are for crawlers.
        self.write_seo_files(root)

    def _write_file(self, path, content: dict) -> None:
        """Write a single JSON file — called from thread pool."""
        path.write_text(
            json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _write_text(self, path, text: str) -> None:
        """Write a single text file (HTML/XML/robots).

        Parent directories are created by the caller via ``_ensure_dir`` (once
        per unique dir) so this stays a pure write — safe to run from many
        worker threads without redundant per-file mkdir calls at scale.
        """
        path.write_text(text, encoding="utf-8")

    @staticmethod
    def _ensure_dir(directory, seen: set) -> None:
        """mkdir -p a directory at most once (cached by string key).

        HTML landing pages share a handful of parent dirs (e.g. one per court
        type), so caching turns 1M+ would-be mkdir calls into a few.
        """
        key = str(directory)
        if key not in seen:
            directory.mkdir(parents=True, exist_ok=True)
            seen.add(key)

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

    # ------------------------------------------------------------------
    # SEO surface: crawlable HTML landing pages + sitemap index + robots.txt
    # ------------------------------------------------------------------

    def write_seo_files(self, root: IndexNode) -> None:
        """Emit crawlable HTML pages, a sitemap index, and robots.txt to the store.

        One HTML landing page per logical document (keyed by document_id under
        ``/d/``), per-dataset child sitemaps (paginated at SITEMAP_MAX_URLS), a
        sitemap index at ``/sitemap.xml``, and a ``/robots.txt`` pointing at it.
        Regenerated every build, so they never drift from the JSON index.

        Streaming pass: documents are rendered and written in a bounded window
        (``_SEO_MAX_INFLIGHT``) and each child sitemap is flushed as it fills, so
        peak memory stays flat whether there are 100 documents or 1M+ — nothing
        accumulates the full set of rendered pages or URLs.
        """
        has_docs = any(
            ms.document_id
            for top in root.children
            for ms in self._iter_manuscripts(top)
        )
        if not has_docs:
            logger.info("SEO: no documents to publish, skipping HTML/sitemap")
            return

        child_sitemap_urls: list[str] = []
        seen_dirs: set[str] = set()
        inflight: deque = deque()
        html_count = 0

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=_MAX_WRITE_WORKERS
        ) as executor:

            def submit_write(path, text) -> None:
                # Bounded window: block on the oldest write once the queue is
                # full so at most _SEO_MAX_INFLIGHT rendered pages are alive.
                inflight.append(executor.submit(self._write_text, path, text))
                if len(inflight) >= _SEO_MAX_INFLIGHT:
                    inflight.popleft().result()

            for top in root.children:
                html_count += self._write_dataset_pages(
                    top, submit_write, child_sitemap_urls, seen_dirs
                )

            # Drain remaining HTML writes (raise on any failure).
            while inflight:
                inflight.popleft().result()

        # Sitemap index + robots.txt at the store root (tiny, write directly).
        self._write_text(
            self.output_root / "sitemap.xml",
            self._sitemap_index_xml(child_sitemap_urls),
        )
        self._write_text(self.output_root / "robots.txt", self._robots_txt())

        logger.info(
            "SEO: wrote %d HTML page(s) + %d sitemap file(s) + robots.txt",
            html_count,
            len(child_sitemap_urls) + 1,
        )

    def _write_dataset_pages(
        self,
        top: IndexNode,
        submit_write,
        child_sitemap_urls: list[str],
        seen_dirs: set,
    ) -> int:
        """Stream one dataset: write each document's HTML page and flush child
        sitemaps as they fill. Returns the number of HTML pages written."""
        dataset = top.name
        page_locs: list[str] = []
        page_num = 0
        html_count = 0

        def flush_sitemap() -> None:
            nonlocal page_num, page_locs
            if not page_locs:
                return
            suffix = "" if page_num == 0 else f".page-{page_num + 1}"
            fname = f"sitemap.{dataset}{suffix}.xml"
            self._write_text(
                self.output_root / fname, self._sitemap_urlset_xml(page_locs)
            )
            child_sitemap_urls.append(f"{self.base_url}/{fname}")
            page_num += 1
            page_locs = []

        for ms in self._iter_manuscripts(top):
            if not ms.document_id:
                continue
            page_rel = self._document_html_relpath(ms.document_id)
            path = self.output_root / page_rel
            self._ensure_dir(path.parent, seen_dirs)
            submit_write(path, self._render_document_html(ms))
            page_locs.append(f"{self.base_url}/{page_rel}")
            html_count += 1
            if len(page_locs) >= SITEMAP_MAX_URLS:
                flush_sitemap()
        flush_sitemap()
        return html_count

    def _iter_manuscripts(self, node: IndexNode):
        """Lazily yield every manuscript under a node (no full list built)."""
        yield from node.manuscripts
        for child in node.children:
            yield from self._iter_manuscripts(child)

    @staticmethod
    def _document_title(ms: Manuscript) -> str:
        meta = ms.metadata or {}
        return str(meta.get("title") or ms.file_name or ms.document_id or "Document")

    @staticmethod
    def _document_description(ms: Manuscript) -> str:
        meta = ms.metadata or {}
        text = meta.get("full_text") or meta.get("title") or ""
        return " ".join(str(text).split())[:300]

    def _render_document_html(self, ms: Manuscript) -> str:
        """Render a self-contained, crawlable HTML landing page for a document."""
        title = self._document_title(ms)
        description = self._document_description(ms)
        canonical = self._document_url(ms.document_id)
        meta = ms.metadata or {}
        pub_date = meta.get("publication_date") or meta.get("date") or ""

        role_labels = {
            SourceLinkRole.RAW.value: "Download document",
            SourceLinkRole.ALTERNATE.value: "Alternate format",
            SourceLinkRole.SOURCE_PAGE.value: "Original source page",
            SourceLinkRole.MARKDOWN.value: "Text transcript (Markdown)",
            SourceLinkRole.PERMALINK.value: "Permalink",
        }
        link_items = []
        for link in ms.links:
            label = role_labels.get(link.get("role"), link.get("role") or "Link")
            href = html.escape(link.get("link", ""), quote=True)
            link_items.append(
                f'<li><a href="{href}" rel="noopener">{html.escape(label)}</a></li>'
            )
        links_html = "\n".join(link_items) or "<li>No files available.</li>"

        has_markdown = any(
            link.get("role") == SourceLinkRole.MARKDOWN.value for link in ms.links
        )
        transcript_html = (
            ""
            if has_markdown
            else "<p><em>A text transcript of this document is being prepared.</em></p>"
        )

        jsonld: dict[str, Any] = {
            "@context": "https://schema.org",
            "@type": "CreativeWork",
            "name": title,
            "url": canonical,
            "identifier": ms.document_id,
            "inLanguage": "ne",
            "isAccessibleForFree": True,
            "publisher": {"@type": "Organization", "name": "Jawafdehi NGM"},
        }
        if pub_date:
            jsonld["datePublished"] = str(pub_date)
        raw_link = next(
            (
                link["link"]
                for link in ms.links
                if link.get("role") == SourceLinkRole.RAW.value
            ),
            "",
        )
        if raw_link:
            jsonld["associatedMedia"] = {"@type": "MediaObject", "contentUrl": raw_link}

        date_html = (
            f"<p><strong>Publication date:</strong> {html.escape(str(pub_date))}</p>"
            if pub_date
            else ""
        )
        full_text = meta.get("full_text")
        body_text = (
            f"<section><p>{html.escape(' '.join(str(full_text).split()))}</p></section>"
            if full_text
            else ""
        )
        a = html.escape  # local alias for brevity below
        # Escape <, >, & so scraped text containing "</script>" (or "<!--")
        # cannot break out of the JSON-LD <script> block. These are valid JSON
        # string escapes, so the structured data still parses — this is the
        # standard guard for inlining JSON into an HTML <script>.
        jsonld_str = (
            json.dumps(jsonld, ensure_ascii=False)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )
        return f"""<!DOCTYPE html>
<html lang="ne">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{a(title)} — Jawafdehi NGM</title>
<meta name="description" content="{a(description, quote=True)}">
<link rel="canonical" href="{a(canonical, quote=True)}">
<meta property="og:type" content="article">
<meta property="og:title" content="{a(title, quote=True)}">
<meta property="og:description" content="{a(description, quote=True)}">
<meta property="og:url" content="{a(canonical, quote=True)}">
<meta name="robots" content="index, follow">
<script type="application/ld+json">{jsonld_str}</script>
</head>
<body>
<main>
<h1>{a(title)}</h1>
{date_html}
{body_text}
{transcript_html}
<h2>Files &amp; sources</h2>
<ul>
{links_html}
</ul>
<p><small>Document ID: {a(ms.document_id)} · Part of the <a href="{a(self.base_url, quote=True)}/index-v2.json">NGM open archive</a>.</small></p>
</main>
</body>
</html>
"""

    def _sitemap_urlset_xml(self, locs: list[str]) -> str:
        """Build a <urlset> sitemap listing document landing-page URLs."""
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        ]
        for loc in locs:
            lines.append("  <url>")
            lines.append(f"    <loc>{html.escape(loc)}</loc>")
            lines.append(f"    <lastmod>{self.date_str}</lastmod>")
            lines.append("  </url>")
        lines.append("</urlset>")
        return "\n".join(lines) + "\n"

    def _sitemap_index_xml(self, sitemap_urls: list[str]) -> str:
        """Build the <sitemapindex> referencing all per-dataset child sitemaps."""
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        ]
        for url in sitemap_urls:
            lines.append("  <sitemap>")
            lines.append(f"    <loc>{html.escape(url)}</loc>")
            lines.append(f"    <lastmod>{self.date_str}</lastmod>")
            lines.append("  </sitemap>")
        lines.append("</sitemapindex>")
        return "\n".join(lines) + "\n"

    def _robots_txt(self) -> str:
        """robots.txt allowing all crawlers and pointing at the sitemap index."""
        return f"User-agent: *\nAllow: /\n\nSitemap: {self.base_url}/sitemap.xml\n"


def get_base_url() -> str:
    """Get the base URL from environment, defaulting to the production ngm store."""
    return os.getenv("NGM_STORE_BASE_URL", "https://ngm-store.jawafdehi.org").rstrip(
        "/"
    )


def _db_index_enabled() -> bool:
    return os.getenv("NGM_SKIP_DB_INDEX", "").lower() not in ("1", "true", "yes")


def main() -> None:
    """Build the index, mirror it into Postgres, and publish it to the store.

    With an ``s3://`` FILES_STORE the derived tree is built to a LOCAL staging dir
    and bulk-uploaded + synced to R2 once at the end (fast local disk, one upload
    pass, stale remote objects pruned). With a local FILES_STORE the tree is
    written in place (no staging/publish) — used by tests and serve_test_index.
    """
    files_store_env = os.getenv("FILES_STORE")
    if not files_store_env:
        logger.error("FILES_STORE environment variable must be set.")
        raise SystemExit(1)

    files_store = str(files_store_env)
    base_url = get_base_url()
    date_str = datetime.now().strftime("%Y-%m-%d")
    remote = files_store.startswith("s3://")

    # Build to local staging when publishing to a cloud store; pinned dirs (set
    # via env) are left in place, ad-hoc temp dirs are cleaned up in finally.
    pinned_staging = os.getenv("INDEX_STAGING_DIR")
    staging_dir = None
    output_path = None
    if remote:
        staging_dir = pinned_staging or tempfile.mkdtemp(prefix="ngm-index-")
        output_path = staging_dir

    logger.info("Building index ....")
    logger.info("Files store (read): %s", files_store)
    logger.info("Output (write): %s", output_path or files_store)
    logger.info("Base URL: %s", base_url)
    logger.info("Date: %s", date_str)

    try:
        builder = IndexBuilder(files_store, base_url, date_str, output_path=output_path)

        root = builder.build_tree()
        logger.info("Tree built successfully")

        try:
            root.validate()
            logger.info("Tree validation passed")
        except ValueError as e:
            logger.error("Tree validation failed: %s", e)
            raise SystemExit(1) from None

        try:
            builder.write_index_files(root)
            logger.info("Index files written to %s", builder.output_root)
        except (OSError, TypeError, RuntimeError) as e:
            logger.error("Failed to write index files: %s", e)
            raise SystemExit(1) from None

        # Safety gate: a build with zero documents is almost always a read/scrape
        # failure (all source dirs missing), not a real empty archive. Publishing
        # it would sync-delete every live HTML page + sitemap, and the DB mirror
        # would (next day) drop every row. Refuse to mirror an empty build.
        total_docs = builder._count_manuscripts_in_tree(root)
        if total_docs == 0:
            logger.error(
                "Build produced 0 documents — skipping DB sync and publish to "
                "avoid wiping the live store (likely a source-read failure)."
            )
            raise SystemExit(1)

        # The Postgres mirror and the R2 publish are independent: a failure in
        # one must not skip the other (both self-heal on the next run). Attempt
        # both, then fail the job non-zero if either errored.
        errors: list[str] = []

        # A per-run timestamp (not just the date) so a same-day rebuild still
        # prunes documents that vanished since an earlier run on the same day.
        build_id = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

        if _db_index_enabled():
            from ngm.index.db_index import sync_document_sources

            try:
                stats = sync_document_sources(root, base_url, build_id)
                logger.info(
                    "document_sources synced: upserted=%d deleted=%d",
                    stats["upserted"],
                    stats["deleted"],
                )
            except Exception:
                logger.exception("Failed to sync document_sources index")
                errors.append("document_sources")

        if remote:
            from ngm.index.publish import publish

            try:
                stats = publish(staging_dir, files_store, date_str)
                logger.info(
                    "Published to %s: uploaded=%d deleted=%d",
                    files_store,
                    stats["uploaded"],
                    stats["deleted"],
                )
            except Exception:
                logger.exception("Failed to publish index to %s", files_store)
                errors.append("publish")

        if errors:
            logger.error("Index build finished with failures: %s", ", ".join(errors))
            raise SystemExit(1)

        logger.info("Index build completed successfully")
    finally:
        if staging_dir and not pinned_staging:
            shutil.rmtree(staging_dir, ignore_errors=True)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    from ngm.logging import setup

    setup()
    main()
