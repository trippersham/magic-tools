"""Puller: Scryfall oracle-tags -> ``raw/oracle_tags``.

Flow (data-architecture §ingest, "bundled + self-refreshing dataset"):
    1. GET ``https://api.scryfall.com/bulk-data/oracle-tags`` for the metadata
       (``updated_at`` + ``download_uri``).
    2. Watermark check: skip if ``updated_at`` is not newer than the last land.
    3. GET the ``download_uri`` JSON (~18 MB), land it to ``raw/oracle_tags``.
    4. FAIL-OPEN: any network/HTTP error falls back to the bundled compressed
       snapshot (``data/snapshots/oracle_tags.json.gz``) — the offline baseline —
       so a caller ALWAYS gets tags. Logs, never crashes.

Snapshot trim: the committed snapshot keeps the FULL 4,499-tag DAG (all
parent/child edges — mandatory, since root tags carry 0 taggings and you must
roll leaves up) but caps taggings at 8/tag (~24.5k of 229.9k) to stay under
~1 MB. The rollup (Phase 4) needs the whole structure; a bounded tagging sample
is enough for the offline baseline. The full daily file refreshes on demand.

Scryfall conventions (mirroring scripts/scryfall_cache.py): a descriptive
``User-Agent`` and a courteous rate-limit pause.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from pipeline import store
from pipeline.ingest._common import Watermark, is_newer

log = logging.getLogger("make_magic.ingest.oracle_tags")

SOURCE = "oracle_tags"
BULK_META_URL = "https://api.scryfall.com/bulk-data/oracle-tags"
# Mirror scripts/scryfall_cache.py: descriptive UA + courteous pacing.
HEADERS = {"User-Agent": "make-magic-plugin/2.0"}
RATE_LIMIT_MS = 100
SNAPSHOT = (
    Path(__file__).resolve().parents[2] / "data" / "snapshots" / "oracle_tags.json.gz"
)


def _fetch_remote(client: httpx.Client) -> tuple[list[dict[str, Any]], str]:
    """Fetch tags from Scryfall. Returns ``(tags, updated_at)``.

    Raises on any HTTP/network failure — the caller catches and falls back.
    """
    meta = client.get(BULK_META_URL, headers=HEADERS, timeout=30)
    meta.raise_for_status()
    meta_json = meta.json()
    updated_at = str(meta_json["updated_at"])
    download_uri = str(meta_json["download_uri"])

    time.sleep(RATE_LIMIT_MS / 1000)
    resp = client.get(download_uri, headers=HEADERS, timeout=120)
    resp.raise_for_status()
    tags = resp.json()
    # Scryfall bulk downloads are a bare JSON array; be defensive if wrapped.
    if isinstance(tags, dict):
        tags = tags.get("data", [])
    return list(tags), updated_at


def _load_snapshot() -> list[dict[str, Any]]:
    """Load the bundled offline snapshot (gzipped JSON array of tags)."""
    with gzip.open(SNAPSHOT, "rt", encoding="utf-8") as f:
        return json.load(f)


def _land(tags: list[dict[str, Any]]) -> Path:
    """Materialize ``tags`` to ``raw/oracle_tags.parquet`` via the store.

    DuckDB infers the (nested) schema from the JSON, so we write the array to a
    temp file and let ``read_json`` land it as Parquet.
    """
    with store.connect() as conn:
        raw_dir = store.StorePaths.resolve().layer_dir("raw", create=True)
        tmp = raw_dir / "_oracle_tags.tmp.json"
        tmp.write_text(json.dumps(tags), encoding="utf-8")
        try:
            rel = conn.read_json(str(tmp))
            path = store.write_parquet(conn, rel, "raw", SOURCE)
        finally:
            tmp.unlink(missing_ok=True)
    return path


def run(*, client: httpx.Client | None = None, force: bool = False) -> Path:
    """Pull oracle-tags into ``raw/oracle_tags``; return the landed Parquet path.

    Watermark-gated (skip-if-not-newer) and FAIL-OPEN: on any fetch failure,
    lands the bundled snapshot instead of raising.
    """
    wm = Watermark.load()
    prior = wm.get(SOURCE)
    owns_client = client is None
    client = client or httpx.Client()
    try:
        tags, updated_at = _fetch_remote(client)
        if not force and not is_newer(prior, updated_at):
            log.info(
                "oracle_tags: %s not newer than %s; skipping land.", updated_at, prior
            )
            # Still ensure a landed table exists (first-run edge covered by is_newer).
            if store.table_exists("raw", SOURCE):
                return store.StorePaths.resolve().parquet_path(
                    "raw", SOURCE, create=False
                )
        path = _land(tags)
        wm.set(SOURCE, updated_at)
        wm.save()
        log.info("oracle_tags: landed %d tags (updated_at=%s).", len(tags), updated_at)
        return path
    except Exception as exc:
        log.warning(
            "oracle_tags: fetch failed (%s); falling back to bundled snapshot.", exc
        )
        tags = _load_snapshot()
        path = _land(tags)
        log.info("oracle_tags: landed %d tags from snapshot.", len(tags))
        return path
    finally:
        if owns_client:
            client.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    path = run()
    print(f"landed oracle_tags -> {path}")


if __name__ == "__main__":
    main()
