"""Phase-A combo-intrinsic edge authoring (ADR 0021 B1).

Closes the Urza thin-capture at the **correct layer**.  A detected combo is
already decomposed by the fid adapter into ``{event, magnitude, scope, route}``
per produced feature -- the same decomposition the win-walk (View A, ADR 0016)
consumes.  This module takes the *other* read of that one decomposition
(ADR 0021 B1): it lowers a combo's fid-classified event set into within-deck
``EventEffect`` couplings and persists them as :class:`.machine_vocab.CardAttribute`
records with ``source='spellbook'`` provenance.

Two phases, one ordering (ADR 0021)
-----------------------------------
- **Phase A (here, process-once, deck-agnostic, cached).**  Authoring is keyed
  by the combo's stable ``variant_id`` and reads only combo-intrinsic inputs:
  the combo's ``result`` fids (View A's own classification), the combo pieces'
  otags (``normalized/card_otag.parquet``, deck-agnostic), and the bounded event
  catalog adjacency.  No deck is consulted.  Edges are cached in the
  :class:`.machine_vocab.ProvisionalAttributeStore`, versioned by
  ``derivation_version`` (idempotent re-run).
- **Phase B (``engine_report``, per-deck).**  Assembly only: it selects the
  cached edges of the combos *present* in the deck and feeds them through the
  existing :func:`.relay.build_relay_augmented_event_edges`.  No re-derivation.

Scope (owner decision, ADR 0021 B1): **combo-piece scope** -- the producer
attributes are drawn only from the attributes carried by the combo's own pieces
(not every catalog producer, which over-fires; not commander-only, which misses
Urza whose loop runs through 99-card attrs).

This module is READ-ONLY with respect to the lake.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pipeline.transforms.strategy.assets import StrategyAssets, load_assets
from pipeline.transforms.strategy.facts import otag_derived_attrs
from pipeline.transforms.strategy.machine_vocab import (
    DERIVATION_VERSION,
    EXTRACTION_VERSION,
    CardAttribute,
    Provenance,
    load_store,
    persist_batch,
)
from pipeline.transforms.strategy.win_walk import (
    fid_route,
    resolve_fids_from_feature_names,
)

#: Provenance source label for a deterministically lowered combo edge.  Reuses
#: the existing ``'spellbook'`` provenance value (ADR 0008: combo-database-derived)
#: — these edges are a deterministic re-view of the fid structure the win-walk
#: already classifies, derived from the Spellbook combo database.  Neither
#: ``curated`` (hand-authored) nor ``haiku`` (LLM).
COMBO_EDGE_SOURCE = 'spellbook'

#: fid_route ``route`` values that mark a recursion-capable (within-loop) event
#: -- the natural trigger set for lowering (prior design Q2: >=2 coupled
#: intermediate events).  ``intermediate`` is the route fid_route assigns to an
#: unbounded, non-terminal catalog event (mana-produced, draw-a-card,
#: creature-enters-battlefield, counter-placed, player-counter, ...).
_RECURSION_ROUTES = frozenset({'intermediate'})


def combo_event_set(result: str, assets: StrategyAssets) -> frozenset[str]:
    """E(C): the combo's recursion-capable catalog events (combo-intrinsic).

    Resolves the combo's ``result`` feature names to fids and classifies each
    via :func:`.win_walk.fid_route` -- the *same* decomposition View A uses.
    Keeps the events whose route is recursion-capable and that exist in the
    bounded event catalog.  Pure function of the combo's result string.
    """
    feature_names = [f.strip() for f in (result or '').split(';') if f.strip()]
    fids = resolve_fids_from_feature_names(feature_names)
    events: set[str] = set()
    for fid in fids:
        if not fid:
            continue
        r = fid_route(fid)
        if r is None:
            continue
        if r.get('route') not in _RECURSION_ROUTES:
            continue
        ev = r.get('event')
        if ev and ev in assets.events:
            events.add(ev)
    return frozenset(events)


def _attr_query(attr: str, assets: StrategyAssets) -> str:
    """A Scryfall-pasteable predicate matching cards carrying attribute *attr*.

    A bucket attribute expands to the OR of its member otag slugs; an
    otag-slug attribute (payoff / event-anchor / ench / edhrec) is itself the
    slug.  Mirrors how :func:`.facts.otag_derived_attrs` recognises the
    attribute, so Phase-B membership (the ``engine_report`` keystone) matches the
    same cards Phase A reasoned over.
    """
    slugs = assets.buckets.get(attr)
    if slugs:
        return ' OR '.join(f'otag:{s}' for s in sorted(set(slugs)))
    return f'otag:{attr}'


def _provenance(
    *,
    combo_variant_id: str,
    source_card_oracle_id: str,
    authored_at: str,
) -> Provenance:
    return Provenance(
        source=COMBO_EDGE_SOURCE,
        authored_at=authored_at,
        source_card_oracle_id=source_card_oracle_id,
        combo_variant_id=combo_variant_id,
        lifecycle='verified',
        extraction_version=EXTRACTION_VERSION,
        derivation_version=DERIVATION_VERSION,
    )


def derive_combo_edges(
    combo: Mapping[str, object],
    otag_index: Mapping[str, set[str]],
    assets: StrategyAssets,
    *,
    authored_at: str = '',
) -> list[CardAttribute]:
    """Lower one combo into its implied within-deck ``EventEffect`` couplings.

    Deterministic, combo-intrinsic.  For each recursion-capable event ``e`` in
    ``E(C)`` and each producer attribute ``a`` of ``e`` that is **carried by a
    combo piece** (combo-piece scope), author a coupling making ``a``'s cards a
    consumer of every *other* event ``e'`` in ``E(C)`` and a producer of ``e``.
    The relay then injects ``a`` as a within-loop relay node, so a one-directional
    producer (e.g. Urza's ``mana-produced``) becomes recursive.

    Parameters
    ----------
    combo
        A combo dict with ``variant_id``, ``oids`` (or ``card_oracle_ids``), and
        ``result``.
    otag_index
        ``oracle_id -> {otag slug}`` (deck-agnostic, from ``card_otag.parquet``).
    assets
        The strategy assets (event catalog + buckets).

    Returns
    -------
    list[CardAttribute]
        The combo's implied edge attributes (empty when ``|E(C)| < 2`` -- a
        single event cannot form a within-loop coupling).
    """
    variant_id = str(combo.get('variant_id', ''))
    result = str(combo.get('result', ''))
    oids = combo.get('oids') or combo.get('card_oracle_ids') or []
    piece_oids = [str(o) for o in oids]  # type: ignore[union-attr]

    events = combo_event_set(result, assets)
    if len(events) < 2:
        return []

    # Combo-piece scope: the union of attributes carried by the combo's pieces.
    piece_attrs: set[str] = set()
    for oid in piece_oids:
        piece_attrs |= otag_derived_attrs(otag_index.get(oid, set()), assets)
    if not piece_attrs:
        return []

    # Producer attributes the combo's pieces carry, per event.  An event the
    # pieces actually PRODUCE anchors the producer end ``a``; its consumer end
    # ranges over every OTHER event in the combo's E(C) (the combo proves those
    # events co-occur in one loop, even when the mirror producer lives on a
    # DIFFERENT combo of the same deck -- infinite mana often emerges from piece
    # interaction, so no single piece is tagged a mana producer).  The spurious
    # (ungrounded) direction is pruned later by the Phase-B reciprocal-grounding
    # filter, which aggregates across the deck's PRESENT combos.
    sorted_events = sorted(events)
    producers_by_event: dict[str, set[str]] = {
        e: (piece_attrs & set(assets.events[e]['produced_by'])) for e in sorted_events
    }
    if not any(producers_by_event.values()):
        return []

    edges: list[CardAttribute] = []
    seen: set[tuple[str, str, str]] = set()
    for e in sorted_events:
        producers = producers_by_event[e]
        for a in sorted(producers):
            for e_prime in sorted_events:
                if e_prime == e:
                    continue
                key = (a, e_prime, e)
                if key in seen:
                    continue
                seen.add(key)
                # The edge is NAMED AFTER the existing producer attribute ``a`` so
                # the relay AUGMENTS the real node already wired into the event
                # graph (inheriting its established mass + position), rather than
                # adding a disconnected synthetic node.  ``a`` natively produces
                # ``e``; the edge adds the consumer(``e_prime``) role that the
                # combo proves, turning the one-directional producer recursive
                # once the reciprocal direction (authored symmetrically below) is
                # also present.  ``ability_key`` records the lowered coupling so
                # the Phase-B assembly can pick a reciprocal-grounded role.
                edges.append(
                    CardAttribute(
                        name=a,
                        query=_attr_query(a, assets),
                        trigger_event=e_prime,
                        produced_event=e,
                        provenance=_provenance(
                            combo_variant_id=variant_id,
                            source_card_oracle_id='',
                            authored_at=authored_at,
                        ),
                    )
                )
    return edges


def derive_self_couplings(
    oracle_id: str,
    otags: set[str],
    assets: StrategyAssets,
    *,
    authored_at: str = '',
) -> list[CardAttribute]:
    """Single-card within-card couplings (ADR 0021 Phase-A, per card).

    A card authors a within-card edge ``e' -> e`` only when it carries, on the
    same card, an attribute that the catalog lists as a **trigger** of ``e'``
    AND an attribute it lists as a **producer** of ``e`` (e' != e).  This is the
    genuinely self-contained relay the fid adapter + otag closure already prove
    for one card -- not a combo, so ``combo_variant_id`` is empty and the edge is
    keyed by ``oracle_id`` (idempotent via ``has_fresh_edge_for_card``).

    .. note:: **Staging (5.2).**  This function is intentionally NOT wired into
       the 5.2 machinery-only commit.  It is tested in isolation
       (``test_combo_edges.py::TestDeriveSelfCouplings``) and will be activated
       by the 5.3 Phase-A process-once driver, which iterates over oracle_ids
       and calls this for each card.
    """
    attrs = otag_derived_attrs(otags, assets)
    if not attrs:
        return []

    triggers_by_event: dict[str, set[str]] = {}
    produced_by_event: dict[str, set[str]] = {}
    for ev, spec in assets.events.items():
        t = attrs & set(spec['triggers'])
        p = attrs & set(spec['produced_by'])
        if t:
            triggers_by_event[ev] = t
        if p:
            produced_by_event[ev] = p

    edges: list[CardAttribute] = []
    seen: set[tuple[str, str, str]] = set()
    for e in sorted(produced_by_event):
        for a in sorted(produced_by_event[e]):
            for e_prime in sorted(triggers_by_event):
                if e_prime == e:
                    continue
                key = (a, e_prime, e)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(
                    CardAttribute(
                        name=f'{COMBO_EDGE_SOURCE}::{a}::{e_prime}=>{e}',
                        query=_attr_query(a, assets),
                        trigger_event=e_prime,
                        produced_event=e,
                        provenance=_provenance(
                            combo_variant_id='',
                            source_card_oracle_id=oracle_id,
                            authored_at=authored_at,
                        ),
                    )
                )
    return edges


def author_combo_edges(
    combos: list[dict],
    otag_index: Mapping[str, set[str]],
    assets: StrategyAssets | None = None,
    *,
    authored_at: str | None = None,
) -> list[CardAttribute]:
    """Author the combo-implied edges for every combo (Phase-A driver core).

    Deterministic + idempotent: the returned records are the combo-intrinsic
    edge set.  Persisting them is the caller's job (via
    :func:`.machine_vocab.persist`, which dedups per
    ``(name, oracle_id, combo_variant_id)``), so re-running at the same
    ``derivation_version`` is a no-op.  Returns a flat, order-stable list.
    """
    assets = assets or load_assets()
    stamp = authored_at or datetime.now(UTC).isoformat()
    out: list[CardAttribute] = []
    for combo in sorted(combos, key=lambda c: str(c.get('variant_id', ''))):
        out.extend(derive_combo_edges(combo, otag_index, assets, authored_at=stamp))
    return out


# ---------------------------------------------------------------------------
# Phase-A population driver (ADR 0021, task 5.3a)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PopulationResult:
    """Summary of a :func:`populate_combo_edges` run.

    Attributes
    ----------
    total_combos
        Number of combos iterated.
    authored
        Number of new edges persisted (0 on a no-op re-run).
    skipped
        Number of combos with fresh edges already in the store (idempotent).
    derived
        Number of combos for which derivation ran (``total_combos - skipped``).
    edges_derived
        Total edge records produced by derivation (includes 0-edge combos).
    """

    total_combos: int
    authored: int
    skipped: int
    derived: int
    edges_derived: int


def build_otag_index_from_lake(
    oracle_parquet_dir: Path | None = None,
) -> dict[str, set[str]]:
    """Build ``oracle_id -> {otag slugs}`` from ``card_otag.parquet`` (deck-agnostic).

    This is the universal otag index the Phase-A population driver needs.  It
    reads the normalized ``card_otag.parquet`` (one row per ``(oracle_id, slug)``
    pair) and groups by ``oracle_id``.

    Parameters
    ----------
    oracle_parquet_dir
        Explicit base directory for the lake.  ``None`` resolves via
        ``MAKE_MAGIC_DATA_DIR`` / XDG default, same as
        :func:`.combo.load_combos_from_lake`.
    """
    import duckdb

    path = _otag_parquet_path(oracle_parquet_dir)
    con = duckdb.connect(':memory:')
    rows = con.execute(f"SELECT oracle_id, slug FROM read_parquet('{path}')").fetchall()
    index: dict[str, set[str]] = {}
    for oid, slug in rows:
        index.setdefault(str(oid), set()).add(slug)
    return index


def _otag_parquet_path(explicit: Path | None) -> Path:
    """Resolve ``card_otag.parquet`` path, same resolution as ``combo.py``."""
    if explicit is not None:
        return Path(explicit) / 'normalized' / 'card_otag.parquet'
    try:
        from pipeline import store as store_mod

        return store_mod.StorePaths.resolve().layer_dir('normalized') / 'card_otag.parquet'
    except Exception:
        import os

        base = os.environ.get('MAKE_MAGIC_DATA_DIR')
        root = Path(base) if base else (Path.home() / '.local' / 'share' / 'make-magic')
        return root / 'normalized' / 'card_otag.parquet'


def populate_combo_edges(
    store_dir: Path | None = None,
    *,
    combos: list[dict] | None = None,
    otag_index: Mapping[str, set[str]] | None = None,
    oracle_parquet_dir: Path | None = None,
) -> PopulationResult:
    """Phase-A population driver: author all combo edges into the store.

    Deterministic, deck-agnostic, cached, idempotent.  Iterates every combo
    in ``combo.parquet`` (or the provided *combos* list), calls
    :func:`derive_combo_edges` for each, and persists the resulting
    :class:`.machine_vocab.CardAttribute` records to the store.

    **Idempotent**: combos whose edges are already fresh in the store at the
    current :data:`DERIVATION_VERSION` are skipped (no re-derivation).  For
    combos that ARE derived, :func:`.machine_vocab.persist` deduplicates at
    the record level, so re-running at the same version is a no-op.

    **Does NOT auto-run inside** ``compute_facts`` / ``resolve()`` (those
    stay frozen/empty-store).  This is a separate Phase-A population step.

    Parameters
    ----------
    store_dir
        Directory for the machine store.  ``None`` uses the default
        ``~/.local/share/make-magic/machine_vocab/``.
    combos
        Pre-loaded combo dicts.  ``None`` loads from ``combo.parquet`` via
        :func:`.combo.load_combos_from_lake`.
    otag_index
        Pre-built ``oracle_id -> {otag slugs}`` index.  ``None`` builds from
        ``card_otag.parquet`` via :func:`build_otag_index_from_lake`.
    oracle_parquet_dir
        Explicit lake base directory (forwarded to combo + otag loaders when
        *combos* / *otag_index* are ``None``).

    Returns
    -------
    PopulationResult
        Summary of the run: how many combos were processed, skipped, and how
        many edges were authored.
    """
    from pipeline.transforms.strategy.combo import load_combos_from_lake

    assets = load_assets()

    if combos is None:
        combos = load_combos_from_lake(oracle_parquet_dir)
    if otag_index is None:
        otag_index = build_otag_index_from_lake(oracle_parquet_dir)

    existing_store = load_store(store_dir)

    stamp = datetime.now(UTC).isoformat()
    skipped = 0
    derived = 0
    edges_derived = 0
    all_edges: list[CardAttribute] = []

    for combo in sorted(combos, key=lambda c: str(c.get('variant_id', ''))):
        vid = str(combo.get('variant_id', ''))

        # Version-aware idempotency: skip if the store already has fresh edges.
        if existing_store.has_fresh_edges_for_combo(
            vid,
            derivation_version=DERIVATION_VERSION,
        ):
            skipped += 1
            continue

        derived += 1
        edges = derive_combo_edges(combo, otag_index, assets, authored_at=stamp)
        edges_derived += len(edges)
        all_edges.extend(edges)

    # Batch-write all new edges in one pass (O(n) dedup, not O(n^2)).
    authored = persist_batch(all_edges, store_dir=store_dir)

    return PopulationResult(
        total_combos=len(combos),
        authored=authored,
        skipped=skipped,
        derived=derived,
        edges_derived=edges_derived,
    )
