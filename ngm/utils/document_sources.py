"""Build court-order ``DocumentSource`` structs for ``court_cases.document_sources``.

Each decided case that has order files gets ONE logical DocumentSource whose
``links`` carry the order files (primary file RAW, the rest ALTERNATE), mirroring
the index's court-order manuscript shape in ``ngm/index/build_index.py``
(``_file_links`` / ``_primary_url`` / ``_build_court_case_leaf_node``) field-for-field,
so a future Jawafdehi-API import is 1:1. ``tests/unit/test_document_sources.py``
pins this to the index output (parity test) so the two cannot drift.

The column holds a *list* of DocumentSource dicts (today always 0 or 1 entry —
one per case — but list-shaped to match the API and allow growth).
"""

import os

from ngm.index.models import SourceLinkRole, SourceType

DEFAULT_STORE_BASE_URL = "https://ngm-store.jawafdehi.org"


def get_store_base_url(base_url: str | None = None) -> str:
    """Public R2 base for building absolute links.

    Same resolution as the index's ``get_base_url`` (``NGM_STORE_BASE_URL`` env,
    defaulting to the prod store domain) — replicated here so the scrape-time
    pipeline doesn't import the heavy index machinery.
    """
    if base_url:
        return base_url.rstrip("/")
    return os.getenv("NGM_STORE_BASE_URL", DEFAULT_STORE_BASE_URL).rstrip("/")


def file_links(file_urls: list[str]) -> list[dict[str, str]]:
    """Roled link list mirroring ``DocumentSource.url``: primary (PDF-preferred)
    is RAW, every other file is ALTERNATE. Stable sort otherwise preserves order.
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


def primary_url(links: list[dict[str, str]]) -> str:
    """Back-compat ``url``: the first link's target (RAW if present)."""
    return links[0]["link"] if links else ""


def order_document_sources(
    court_identifier: str,
    case_number: str,
    file_paths: list[str],
    base_url: str | None = None,
) -> list[dict]:
    """Build the ``document_sources`` value for one case from its stored R2 paths.

    Args:
        court_identifier: e.g. ``"supreme"`` / ``"special"`` (also the path segment
            and the ``document_id`` court_type, matching the index).
        case_number: e.g. ``"082-WO-0123"``.
        file_paths: store-relative paths as written by ``SupremeCourtOrdersPipeline``
            (e.g. ``["court-orders/supreme/082-WO-0123.1.pdf"]``).
        base_url: override the public store base (defaults to ``NGM_STORE_BASE_URL``).

    Returns:
        A list with one DocumentSource dict, or ``[]`` if there are no files.
    """
    if not file_paths:
        return []
    base = get_store_base_url(base_url)
    file_urls = [f"{base}/{p.lstrip('/')}" for p in file_paths if p]
    links = file_links(file_urls)
    if not links:
        return []
    return [
        {
            "document_id": f"ngm:court-order:{court_identifier}:{case_number}",
            "source_type": SourceType.COURT_ORDER.value,
            "url": primary_url(links),
            "links": links,
        }
    ]
