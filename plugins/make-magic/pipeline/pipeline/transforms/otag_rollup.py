"""Roll each card's leaf oracle-tags up the tag DAG and land ``card_otag``.

THE central Phase-4 transform. Scryfall's oracle-tags form a DAG (IS-A /
broader<->narrower) whose ROOT tags carry ~0 direct taggings — cards carry only
LEAF tags. So to see anything you must roll each card's leaves up to ALL their
ancestors (research §"CRITICAL gotcha").

The graph rollup is done in PURE PYTHON (clean + unit-testable):

    1. Read ``raw/oracle_tags`` (id, slug, parent_ids, taggings).
    2. Build ``parent_ids`` adjacency + a ``card -> {leaf tag id}`` map.
    3. For each tag, compute its ANCESTOR CLOSURE with a cycle-safe visited-set
       (multi-parent, so it's a set walk, not a tree walk; the DAG is acyclic
       but we defend against a would-be cycle anyway).
    4. Explode: for every ``(oracle_id, leaf)`` emit ``(oracle_id, slug)`` for
       the leaf AND every ancestor.

The exploded long-form ``(oracle_id, slug)`` table is landed to
``normalized/card_otag`` via ``store`` (SQL/DuckDB owns the later joins;
this module owns the graph maths).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pipeline import store

log = logging.getLogger("make_magic.transforms.otag_rollup")

RAW_SOURCE = "oracle_tags"
NORMALIZED_TABLE = "card_otag"


# --------------------------------------------------------------------------- #
# Pure graph maths — no I/O. Unit-tested against a synthetic DAG.
# --------------------------------------------------------------------------- #


def ancestors(tid: str, parents: dict[str, list[str]]) -> set[str]:
    """Return every ancestor of ``tid`` (exclusive of ``tid`` itself).

    Multi-parent aware and cycle-safe: a visited-set bounds the walk, so a
    would-be cycle (or a shared grandparent reached by two paths) is visited
    once and never loops. ``parents`` maps a tag id to its ``parent_ids``.
    """
    seen: set[str] = set()
    stack = [tid]
    while stack:
        node = stack.pop()
        for parent in parents.get(node, ()):
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen


def closure(tid: str, parents: dict[str, list[str]]) -> set[str]:
    """The tag itself plus all its ancestors (the full rollup set for a leaf)."""
    return {tid} | ancestors(tid, parents)


def rollup_rows(
    tags: list[dict],
) -> list[tuple[str, str]]:
    """Explode ``tags`` into distinct ``(oracle_id, slug)`` rollup rows.

    Each ``tag`` dict has ``id``, ``slug``, ``parent_ids`` and ``taggings``
    (each tagging carries an ``oracle_id``). For every card that carries a leaf
    tag, we emit a row for that leaf's slug AND every ancestor's slug — so a card
    tagged only ``sweeper`` shows up under ``sweeper``, ``removal``, etc.

    Returns a de-duplicated list of ``(oracle_id, slug)`` tuples. IDs/oracle_ids
    are normalized to ``str`` (DuckDB hands back ``uuid.UUID``); the durable join
    key is the string oracle_id, the human-auditable label is the slug.
    """
    by_id: dict[str, dict] = {}
    parents: dict[str, list[str]] = {}
    slug_of: dict[str, str] = {}
    for tag in tags:
        tid = str(tag["id"])
        by_id[tid] = tag
        parents[tid] = [str(p) for p in (tag.get("parent_ids") or [])]
        slug_of[tid] = str(tag.get("slug") or tid)

    # card -> {leaf tag id}
    card_leaf: dict[str, set[str]] = {}
    for tag in tags:
        tid = str(tag["id"])
        for tg in tag.get("taggings") or []:
            oid = tg.get("oracle_id")
            if oid is None:
                continue
            card_leaf.setdefault(str(oid), set()).add(tid)

    # Memoize each leaf's closure — a card shares leaves across the whole set.
    closure_cache: dict[str, set[str]] = {}
    rows: set[tuple[str, str]] = set()
    for oid, leaves in card_leaf.items():
        for leaf in leaves:
            if leaf not in closure_cache:
                closure_cache[leaf] = closure(leaf, parents)
            for tid in closure_cache[leaf]:
                slug = slug_of.get(tid)
                if slug is not None:
                    rows.add((oid, slug))
    return sorted(rows)


# --------------------------------------------------------------------------- #
# I/O — read raw tags, land the exploded normalized table.
# --------------------------------------------------------------------------- #


def _load_raw_tags() -> list[dict]:
    """Read ``raw/oracle_tags`` back as plain dicts (id/slug/parent_ids/taggings)."""
    with store.connect() as conn:
        rel = store.read_parquet(conn, "raw", RAW_SOURCE)
        cols = ["id", "slug", "parent_ids", "taggings"]
        rows = rel.select(", ".join(cols)).fetchall()
    tags: list[dict] = []
    for tid, slug, parent_ids, taggings in rows:
        tags.append(
            {
                "id": tid,
                "slug": slug,
                "parent_ids": parent_ids or [],
                "taggings": taggings or [],
            }
        )
    return tags


def _land(rows: list[tuple[str, str]]) -> Path:
    """Land ``(oracle_id, slug)`` rollup rows to ``normalized/card_otag``."""
    payload = [{"oracle_id": oid, "slug": slug} for oid, slug in rows]
    with store.connect() as conn:
        norm_dir = store.StorePaths.resolve().layer_dir("normalized", create=True)
        tmp = norm_dir / f"_{NORMALIZED_TABLE}.tmp.json"
        # Explicit schema keeps an empty table well-typed (read_json would guess).
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        try:
            if payload:
                rel = conn.read_json(str(tmp))
            else:
                rel = conn.sql(
                    "SELECT NULL::VARCHAR AS oracle_id, NULL::VARCHAR AS slug WHERE 1=0"
                )
            path = store.write_parquet(conn, rel, "normalized", NORMALIZED_TABLE)
        finally:
            tmp.unlink(missing_ok=True)
    return path


def build() -> Path:
    """Read ``raw/oracle_tags``, roll up the DAG, land ``normalized/card_otag``.

    Returns the landed Parquet path.
    """
    tags = _load_raw_tags()
    rows = rollup_rows(tags)
    path = _land(rows)
    log.info(
        "card_otag: rolled %d tags into %d (oracle_id, slug) rows.",
        len(tags),
        len(rows),
    )
    return path


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    path = build()
    print(f"landed card_otag -> {path}")


if __name__ == "__main__":
    main()
