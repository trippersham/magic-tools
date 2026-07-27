"""Puller: Commander Spellbook combos (variants) -> ``raw/combos``.

Commander Spellbook data is MIT-licensed. Two endpoints exist:
    - bulk ``https://json.commanderspellbook.com/variants.json`` (~600 MB; too
      big to fetch/bundle) — but its HTTP ``Last-Modified``/``ETag`` headers are
      the cheap incremental watermark signal.
    - paginated backend ``https://backend.commanderspellbook.com/variants/``
      (``results`` + ``next``) — bounded, fetch-friendly.

Flow:
    1. HEAD the bulk file for ``Last-Modified``/``ETag`` -> the watermark token.
    2. Skip if not newer than the last land.
    3. Page the backend API (bounded by ``max_combos``) and land to
       ``raw/combos``.
    4. FAIL-OPEN: any failure falls back to the bundled snapshot
       (``data/snapshots/combos.json.gz`` — first 2,000 variants).

Snapshot trim: the full variant set is ~600 MB, so the committed offline
baseline is the first 2,000 variants (~1 MB gzipped) — enough to exercise the
combo-detection path offline. The full set refreshes on demand.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from pipeline import store
from pipeline.ingest._common import Watermark, dedupe, is_newer

log = logging.getLogger("make_magic.ingest.spellbook")

SOURCE = "combos"
BULK_URL = "https://json.commanderspellbook.com/variants.json"
API_URL = "https://backend.commanderspellbook.com/variants/"
HEADERS = {"User-Agent": "make-magic-plugin/2.0"}
PAGE_SIZE = 250
DEFAULT_MAX_COMBOS = 2000
SNAPSHOT = Path(__file__).resolve().parents[2] / "data" / "snapshots" / "combos.json.gz"


def _remote_watermark(client: httpx.Client) -> str | None:
    """The bulk file's ``ETag`` (or ``Last-Modified``) — the change signal."""
    resp = client.head(BULK_URL, headers=HEADERS, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return resp.headers.get("etag") or resp.headers.get("last-modified")


def _fetch_remote(client: httpx.Client, max_combos: int) -> list[dict[str, Any]]:
    """Page the backend variants API up to ``max_combos`` results."""
    combos: list[dict[str, Any]] = []
    url: str | None = f"{API_URL}?limit={PAGE_SIZE}"
    while url and len(combos) < max_combos:
        resp = client.get(url, headers=HEADERS, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        combos.extend(payload.get("results", []))
        url = payload.get("next")
    return combos[:max_combos]


def _load_snapshot() -> list[dict[str, Any]]:
    """Load the bundled offline snapshot (gzipped JSON array of variants)."""
    with gzip.open(SNAPSHOT, "rt", encoding="utf-8") as f:
        return json.load(f)


def _land(combos: list[dict[str, Any]]) -> Path:
    """Land combos to ``raw/combos.parquet`` (deduped on ``id``, last-wins)."""
    combos = dedupe(combos, key="id")
    with store.connect() as conn:
        raw_dir = store.StorePaths.resolve().layer_dir("raw", create=True)
        tmp = raw_dir / "_combos.tmp.json"
        tmp.write_text(json.dumps(combos), encoding="utf-8")
        try:
            rel = conn.read_json(str(tmp))
            path = store.write_parquet(conn, rel, "raw", SOURCE)
        finally:
            tmp.unlink(missing_ok=True)
    return path


def run(
    *,
    client: httpx.Client | None = None,
    force: bool = False,
    max_combos: int = DEFAULT_MAX_COMBOS,
) -> Path:
    """Pull combos into ``raw/combos``; return the landed Parquet path.

    Watermark-gated on the bulk file's ETag/Last-Modified and FAIL-OPEN to the
    bundled snapshot on any fetch failure.
    """
    wm = Watermark.load()
    prior = wm.get(SOURCE)
    owns_client = client is None
    client = client or httpx.Client()
    try:
        token = _remote_watermark(client)
        if (
            not force
            and not is_newer(prior, token)
            and store.table_exists("raw", SOURCE)
        ):
            log.info("combos: %s not newer than %s; skipping land.", token, prior)
            return store.StorePaths.resolve().parquet_path("raw", SOURCE, create=False)
        combos = _fetch_remote(client, max_combos)
        path = _land(combos)
        if token is not None:
            wm.set(SOURCE, token)
            wm.save()
        log.info("combos: landed %d variants (watermark=%s).", len(combos), token)
        return path
    except Exception as exc:
        log.warning("combos: fetch failed (%s); falling back to bundled snapshot.", exc)
        combos = _load_snapshot()
        path = _land(combos)
        log.info("combos: landed %d variants from snapshot.", len(combos))
        return path
    finally:
        if owns_client:
            client.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    path = run()
    print(f"landed combos -> {path}")


if __name__ == "__main__":
    main()
