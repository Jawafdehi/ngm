"""
Publish the locally-staged NGM index tree to Cloudflare R2.

``build_index`` writes the derived tree (index JSON, HTML landing pages,
sitemaps, robots.txt) to a local staging dir; this module uploads it to the R2
store in one bulk pass at the end and syncs deletions so the remote stays a
clean mirror instead of accumulating orphans as documents come and go.

Mirror scope — only the SEO surface and the CURRENT index snapshot are synced:
``d/`` (HTML pages), root-level ``sitemap*.xml`` / ``robots.txt`` /
``index-v2.json``, and ``indices/<date>/``. Scraper-owned ``uploads/`` and older
``indices/<date>/`` snapshots (pruned by ``ngm.index.cleanup``) are NEVER listed
or deleted — ``is_safe_to_delete`` is the defensive guard that enforces this.
"""

import concurrent.futures
import itertools
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

_UPLOAD_WORKERS = 16
_DELETE_BATCH = 1000  # S3 delete_objects hard limit

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".xml": "application/xml",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}


# --- pure helpers (unit-tested without network) ------------------------------


def content_type_for(key: str) -> str:
    """MIME type for a stored key by extension.

    Explicit so R2's public domain serves crawlable ``text/html`` instead of the
    ``application/octet-stream`` a blind upload would set.
    """
    for ext, ctype in _CONTENT_TYPES.items():
        if key.endswith(ext):
            return ctype
    return "application/octet-stream"


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """``s3://bucket/prefix`` -> (bucket, prefix with trailing slash or '')."""
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"not an s3:// uri: {uri!r}")
    prefix = parsed.path.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return parsed.netloc, prefix


def is_safe_to_delete(rel_key: str, date_str: str) -> bool:
    """Whether a store-relative key is inside the managed mirror set.

    The single safety gate on remote deletion. Returns True only for the SEO
    surface and the current index snapshot; False for everything else — most
    importantly ``uploads/`` (source data) and other dates' ``indices/``.
    """
    if rel_key.startswith("uploads/"):
        return False
    if rel_key.startswith("d/"):
        return True
    if rel_key.startswith(f"indices/{date_str}/"):
        return True
    if rel_key.startswith("indices/"):
        return False  # a different date's snapshot — leave it for cleanup.py
    if (
        "/" not in rel_key
        and rel_key.startswith("sitemap")
        and rel_key.endswith(".xml")
    ):
        return True
    return False


def compute_delete_set(
    local_keys: set[str], remote_keys: set[str], date_str: str
) -> set[str]:
    """Remote keys to delete: remote-only AND inside the managed mirror set."""
    return {key for key in remote_keys - local_keys if is_safe_to_delete(key, date_str)}


def managed_scan_prefixes(date_str: str) -> list[str]:
    """Store-relative prefixes to list remotely — never the whole bucket.

    Scoped so a sync never enumerates the (potentially millions of) ``uploads/``
    keys: only the SEO surface and the current snapshot are scanned.
    """
    return ["d/", f"indices/{date_str}/", "sitemap"]


def local_keys(staging_dir: Path) -> set[str]:
    """All file keys under the staging dir, store-relative, posix-style."""
    root = Path(staging_dir)
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


# --- boto3 execution ---------------------------------------------------------


def _make_client(endpoint_url: str | None):
    # Size the connection pool to the upload worker count. boto3 defaults to 10,
    # but _upload runs _UPLOAD_WORKERS threads — a smaller pool thrashes
    # ("Connection pool is full, discarding connection") and slows the upload.
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or os.getenv("AWS_ENDPOINT_URL"),
        region_name=os.getenv("AWS_REGION", "auto"),
        config=Config(max_pool_connections=_UPLOAD_WORKERS),
    )


def _upload(client, bucket, prefix, staging_dir: Path, keys: set[str]) -> int:
    staging_dir = Path(staging_dir)

    def put(rel_key: str) -> None:
        client.upload_file(
            str(staging_dir / rel_key),
            bucket,
            f"{prefix}{rel_key}",
            ExtraArgs={"ContentType": content_type_for(rel_key)},
        )

    done = 0
    total = len(keys)
    key_iter = iter(keys)
    with concurrent.futures.ThreadPoolExecutor(max_workers=_UPLOAD_WORKERS) as ex:
        # Bounded window: keep ~4x workers in flight rather than scheduling one
        # Future per file up front (1M files would mean 1M queued Futures).
        inflight = {
            ex.submit(put, k): k
            for k in itertools.islice(key_iter, _UPLOAD_WORKERS * 4)
        }
        while inflight:
            completed, _ = concurrent.futures.wait(
                inflight, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for fut in completed:
                del inflight[fut]
                fut.result()  # raise on first failure
                done += 1
                if done % 5000 == 0:
                    logger.info("  uploaded %d/%d files", done, total)
            for k in itertools.islice(key_iter, len(completed)):
                inflight[ex.submit(put, k)] = k
    logger.info("Uploaded %d file(s) to s3://%s/%s", total, bucket, prefix)
    return total


def _list_remote_keys(client, bucket, prefix, date_str) -> set[str]:
    """List remote keys under the managed scan prefixes, store-relative."""
    found: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for scan in managed_scan_prefixes(date_str):
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}{scan}"):
            for obj in page.get("Contents", []):
                found.add(obj["Key"][len(prefix) :])
    return found


def _delete(client, bucket, prefix, keys: set[str]) -> int:
    keys = sorted(keys)
    errors: list = []
    for i in range(0, len(keys), _DELETE_BATCH):
        batch = keys[i : i + _DELETE_BATCH]
        resp = client.delete_objects(
            Bucket=bucket,
            Delete={
                "Objects": [{"Key": f"{prefix}{k}"} for k in batch],
                "Quiet": True,
            },
        )
        # delete_objects returns 200 even when individual keys fail; the failures
        # are reported per-object under "Errors". Surface them instead of
        # silently leaving orphans behind.
        errors.extend(resp.get("Errors", []) if resp else [])
    if errors:
        raise RuntimeError(
            f"delete_objects failed for {len(errors)} object(s): {errors[:5]}"
        )
    return len(keys)


def publish(
    staging_dir: str, store_uri: str, date_str: str, *, endpoint_url: str | None = None
) -> dict:
    """Upload the staged tree to ``store_uri`` then delete stale managed objects.

    Returns counts: ``{"uploaded": N, "deleted": M}``.
    """
    bucket, prefix = parse_s3_uri(store_uri)
    client = _make_client(endpoint_url)
    keys = local_keys(Path(staging_dir))
    logger.info("Publishing %d staged file(s) to s3://%s/%s", len(keys), bucket, prefix)

    uploaded = _upload(client, bucket, prefix, Path(staging_dir), keys)

    remote = _list_remote_keys(client, bucket, prefix, date_str)
    stale = compute_delete_set(keys, remote, date_str)
    if stale:
        logger.info("Deleting %d stale remote object(s)", len(stale))
        _delete(client, bucket, prefix, stale)
    else:
        logger.info("No stale remote objects to delete")

    return {"uploaded": uploaded, "deleted": len(stale)}
