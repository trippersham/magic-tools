"""Provisional attribute store -- the separate, freezable layer.

This module is the NAMESPACE for provisionally-authored vocab items:
CardAttributes + within-card edges + provenance, produced by the engine-
identification pipeline's Tier-2 hydration pass and verified by the deterministic
gate before persistence.  Sources include ``'haiku'`` (LLM-authored),
``'spellbook'`` (combo-database-derived), and ``'curated'`` (hand-authored,
which live in :mod:`.vocab` but share the provenance enum).

Separation invariant
--------------------
Provisionally-authored vocab is **always distinguishable** from the curated base
(:mod:`.vocab`). Curated-base tests pin and ignore this store (or pin a frozen
snapshot), so accumulated hydration can never retroactively flake deterministic
tests. The store is **freezable**: tests that assert engine output do so against
a frozen store snapshot, never the live growing store.

On-disk format
--------------
The durable store is a **JSONL** file (one JSON object per line) at
``<store_dir>/attributes.jsonl``.  Each line is a self-contained record with
all ``CardAttribute`` + ``Provenance`` fields, human-inspectable::

    {"name": "draw-to-drain", "query": "oracle:/...", "trigger_event": "draw-a-card",
     "produced_event": "life-loss-drain", "source": "haiku", "model_id": "claude-3-5-haiku-20241022",
     "authored_at": "2026-11-06T10:00:00Z", "source_card_name": "Queza, Augur of Agonies",
     "source_card_oracle_id": "abc-123", "deck_context": "queza-esper-drain",
     "scryfall_query": "oracle:/...", "scryfall_resolved_count": 4,
     "verified_at": "2026-11-06T10:00:05Z"}

Curated vs authored is always distinguishable by the ``source`` field
(``'haiku'``/``'spellbook'`` vs ``'curated'``).

Idempotency
-----------
``persist()`` deduplicates on ``(name, source_card_oracle_id)``: the same
attribute from the same source card is never written twice.  A second persist
of an identical key is a no-op.

How tests ignore it
-------------------
Tests that pin curated-base parity import the curated vocab directly from
:mod:`.vocab` and never consult this store. Engine-output tests that need
machine-authored vocab pin a **frozen snapshot** (a committed JSON fixture)
loaded via :func:`load_frozen_snapshot`::

    from pipeline.transforms.strategy.machine_vocab import load_frozen_snapshot
    store = load_frozen_snapshot(Path('tests/fixtures/machine_vocab_frozen.json'))

The empty store (:func:`empty_store`) is inert: it contributes zero attributes,
zero edges, and does not shift the curated base's output. Any code path that
merges machine vocab into the active asset bundle should short-circuit on
``store.is_empty``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Version constants (ADR 0021 Phase A -- two-layer staleness key)
# ---------------------------------------------------------------------------

#: Bump when text-to-features extraction logic or prompt changes.
#: This is the only layer that can trigger a haiku re-run (and then only for
#: candidate cards whose text could carry a newly-added concept).
EXTRACTION_VERSION: int = 1

#: Bump when features-to-events/edges derivation logic changes (pure
#: deterministic re-walk on catalog/adapter change -- no haiku).
DERIVATION_VERSION: int = 1


@dataclass(frozen=True)
class Provenance:
    """Tracks the origin of a machine-authored vocab item.

    Every machine-authored attribute carries provenance so curated and machine
    vocab are always distinguishable, stale otags can trigger re-verification,
    and a wrong edge can be traced and reverted.

    Fields
    ------
    source
        ``'haiku'`` for LLM-authored, ``'spellbook'`` for combo-database-derived,
        ``'curated'`` for hand-authored (curated items live in :mod:`.vocab`, not
        here, but the enum is shared).  Curated vs authored is always distinguishable.
    model_id
        The model that authored the item (e.g. ``'claude-3-5-haiku-20241022'``).
    authored_at
        ISO-8601 timestamp of authoring.
    source_card_name, source_card_oracle_id
        The card that prompted the authoring (name + oracle_id).
    deck_context
        The deck name/slug the authoring arose in.
    scryfall_query
        The verification query pasted to Scryfall.
    scryfall_resolved_count
        How many cards the query matched on Scryfall.
    verified_at
        ISO-8601 timestamp of verification-gate pass.
    lifecycle
        ``'provisional'`` for unverified/machine-authored, ``'verified'`` for
        curated or gate-passed. Default ``'verified'`` (safe conservative).
    """

    source: str                          # 'haiku' | 'spellbook' | 'curated'
    model_id: str = ''
    authored_at: str = ''
    source_card_name: str = ''
    source_card_oracle_id: str = ''
    deck_context: str = ''
    scryfall_query: str = ''
    scryfall_resolved_count: int = 0
    verified_at: str = ''
    breadth_warning: bool = False
    lifecycle: str = 'verified'          # 'provisional' | 'verified'

    #: Extraction-logic version that produced this record (ADR 0021).
    #: ``0`` means "pre-versioning / unknown" (legacy record, always stale).
    extraction_version: int = 0

    #: Derivation-logic version that produced this record (ADR 0021).
    #: ``0`` means "pre-versioning / unknown" (legacy record, always stale).
    derivation_version: int = 0


@dataclass(frozen=True)
class CardAttribute:
    """A single machine-authored CardAttribute with event roles + provenance.

    The query is a Scryfall-pasteable deterministic predicate (same form as
    curated synth attributes in :data:`.vocab.SYNTH_ALWAYS_ATTRIBUTES`).
    ``trigger_event`` / ``produced_event`` are the within-card edge roles the P2
    relay-node derivation reads to inject the card as consumer(trigger) +
    producer(effect) into the event graph.

    Parameters
    ----------
    name
        Attribute name (e.g. ``'draw-to-drain'``).
    query
        Scryfall-pasteable query string.
    trigger_event
        The event this card's trigger ability CONSUMES (e.g. ``'draw'``).
    produced_event
        The event this card's effect ability PRODUCES (e.g. ``'life-loss'``).
        Renamed from ``effect_event`` in Phase 2A.
    provenance
        Origin metadata (always present on machine-authored items).
    """

    name: str
    query: str
    trigger_event: str = ''
    produced_event: str = ''
    provenance: Provenance = field(
        default_factory=lambda: Provenance(source='haiku', lifecycle='provisional'))


@dataclass
class ProvisionalAttributeStore:
    """In-memory representation of the machine-authored vocab layer.

    An empty store is inert: it contributes zero attributes, zero edges, and
    does not shift the curated base's output.
    """

    attributes: list[CardAttribute] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when the store contributes nothing (the P0 default)."""
        return len(self.attributes) == 0

    def get(self, name: str) -> CardAttribute | None:
        """Look up a machine-authored attribute by name."""
        for attr in self.attributes:
            if attr.name == name:
                return attr
        return None

    def names(self) -> frozenset[str]:
        """The set of machine-authored attribute names in this store."""
        return frozenset(a.name for a in self.attributes)

    def has_edge_for_card(self, source_card_oracle_id: str) -> bool:
        """Check whether an edge for this source card already exists.

        The compounding read path uses this to skip re-authoring: if a card's
        oracle_id already has a persisted edge, a second deck containing that
        card reuses the stored edge with no haiku call.
        """
        return any(
            a.provenance.source_card_oracle_id == source_card_oracle_id
            for a in self.attributes
        )

    def has_fresh_edge_for_card(
        self,
        source_card_oracle_id: str,
        *,
        extraction_version: int = EXTRACTION_VERSION,
        derivation_version: int = DERIVATION_VERSION,
    ) -> bool:
        """Version-aware freshness check (ADR 0021 Phase A staleness key).

        Returns ``True`` only when the store contains at least one edge for
        the given ``oracle_id`` **and** every such edge's stored versions
        are ``>=`` the requested versions.  A record stamped with an older
        version is **stale** and needs reprocessing (but is NOT deleted here;
        the 5.3 driver decides reprocessing).

        Unlike :meth:`has_edge_for_card` (which is version-agnostic and
        always available), this method is the gate the Phase-A process-once
        driver uses to decide whether a card needs re-extraction/re-derivation.
        """
        matching = [
            a for a in self.attributes
            if a.provenance.source_card_oracle_id == source_card_oracle_id
        ]
        if not matching:
            return False
        return all(
            a.provenance.extraction_version >= extraction_version
            and a.provenance.derivation_version >= derivation_version
            for a in matching
        )

    def edge_queries(self) -> list[tuple[str, str]]:
        """Return ``(name, query)`` pairs for all machine-authored edge attrs.

        The format matches curated edge queries from
        :func:`.engine_hydration.curated_edge_queries`, so the two can be
        concatenated directly for the ``authored_edges`` argument to
        :func:`.engine_hydration.frontier`.
        """
        return [(a.name, a.query) for a in self.attributes]


# ---------------------------------------------------------------------------
# Serialization helpers (JSONL on-disk format)
# ---------------------------------------------------------------------------

def _attr_to_record(attr: CardAttribute) -> dict:
    """Flatten a CardAttribute + Provenance into a single JSON-serializable dict.

    The record is one line in the JSONL store.  All provenance fields are
    promoted to the top level (no nesting) for human readability and grep-ability.
    """
    prov = asdict(attr.provenance)
    rec = {
        'name': attr.name,
        'query': attr.query,
        'trigger_event': attr.trigger_event,
        'produced_event': attr.produced_event,
    }
    rec.update(prov)
    return rec


def _record_to_attr(rec: dict) -> CardAttribute:
    """Reconstruct a CardAttribute from a flat JSONL record dict."""
    source = rec.get('source', 'haiku')
    prov = Provenance(
        source=source,
        model_id=rec.get('model_id', ''),
        authored_at=rec.get('authored_at', ''),
        source_card_name=rec.get('source_card_name', ''),
        source_card_oracle_id=rec.get('source_card_oracle_id', ''),
        deck_context=rec.get('deck_context', ''),
        scryfall_query=rec.get('scryfall_query', ''),
        scryfall_resolved_count=rec.get('scryfall_resolved_count', 0),
        verified_at=rec.get('verified_at', ''),
        breadth_warning=rec.get('breadth_warning', False),
        lifecycle=rec.get('lifecycle', 'provisional' if source == 'haiku' else 'verified'),
        extraction_version=rec.get('extraction_version', 0),
        derivation_version=rec.get('derivation_version', 0),
    )
    return CardAttribute(
        name=rec['name'],
        query=rec['query'],
        trigger_event=rec.get('trigger_event', ''),
        produced_event=rec.get('produced_event', '') or rec.get('effect_event', ''),
        provenance=prov,
    )


# ---------------------------------------------------------------------------
# Store API
# ---------------------------------------------------------------------------

#: Default store location (XDG data home / make-magic).
_DEFAULT_STORE_DIR = Path.home() / '.local' / 'share' / 'make-magic' / 'machine_vocab'

#: Filename within the store directory.
_STORE_FILENAME = 'attributes.jsonl'


def empty_store() -> ProvisionalAttributeStore:
    """Return a guaranteed-empty machine store (the identity element).

    Code that merges machine vocab into the active asset bundle should use this
    as the default when no store is loaded, and short-circuit on ``is_empty``.
    """
    return ProvisionalAttributeStore()


def load_store(store_dir: Path | str | None = None) -> ProvisionalAttributeStore:
    """Load the durable machine-authored vocab store from disk.

    Reads every line of ``<store_dir>/attributes.jsonl`` (JSONL: one JSON
    object per line) and reconstructs the in-memory :class:`ProvisionalAttributeStore`.

    Returns an **empty** store if the directory does not exist, the file is
    missing, or the file is empty.

    Parameters
    ----------
    store_dir
        Directory containing the store.  Accepts ``str`` or ``Path``.
        Defaults to ``~/.local/share/make-magic/machine_vocab/``.
    """
    if store_dir is not None:
        store_dir = Path(store_dir)
    store_path = (store_dir or _DEFAULT_STORE_DIR) / _STORE_FILENAME
    if not store_path.exists():
        return ProvisionalAttributeStore()

    attrs: list[CardAttribute] = []
    text = store_path.read_text(encoding='utf-8').strip()
    if not text:
        return ProvisionalAttributeStore()

    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            rec = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f'{store_path}:{line_no}: invalid JSON: {exc}'
            ) from exc
        attrs.append(_record_to_attr(rec))

    return ProvisionalAttributeStore(attributes=attrs)


def load_frozen_snapshot(path: Path) -> ProvisionalAttributeStore:
    """Load a frozen machine-store snapshot from a test fixture.

    Engine-output tests pin against a frozen snapshot (a committed JSON fixture),
    not the live growing store.

    The snapshot file is a **JSON array** of flat record dicts (the same shape
    as individual JSONL lines, but wrapped in ``[...]`` for readability as a
    fixture).
    """
    if not path.exists():
        raise FileNotFoundError(f'Frozen snapshot not found: {path}')

    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, list):
        raise ValueError(f'Frozen snapshot must be a JSON array, got {type(data).__name__}')

    attrs = [_record_to_attr(rec) for rec in data]
    return ProvisionalAttributeStore(attributes=attrs)


def persist(attr: CardAttribute, store_dir: Path | str | None = None) -> None:
    """Persist a verified CardAttribute to the durable store.

    Appends a single JSONL record to ``<store_dir>/attributes.jsonl``.
    Creates the directory and file if they do not exist.

    **Idempotency:** deduplicates on ``(name, source_card_oracle_id)`` -- the
    same attribute derived from the same source card is never written twice.
    A second ``persist()`` call with the same dedup key is a silent no-op.

    Parameters
    ----------
    attr
        A verified :class:`CardAttribute`.  The caller (the verification
        gate) is responsible for ensuring the attribute passed all checks
        before calling persist.
    store_dir
        Directory to write the store into.  Accepts ``str`` or ``Path``.
        Defaults to ``~/.local/share/make-magic/machine_vocab/``.

    Raises
    ------
    ValueError
        If the attribute has ``source='curated'`` (curated attrs belong in
        :mod:`.vocab`, not the machine store).
    """
    if attr.provenance.source == 'curated':
        raise ValueError(
            f'Cannot persist curated attribute {attr.name!r} to the machine '
            f'store.  Curated attrs belong in vocab.py.'
        )

    if store_dir is not None:
        store_dir = Path(store_dir)
    resolved_dir = store_dir or _DEFAULT_STORE_DIR
    store_path = resolved_dir / _STORE_FILENAME

    # --- Idempotency check: load existing dedup keys ----------------------
    dedup_key = (attr.name, attr.provenance.source_card_oracle_id)
    if store_path.exists():
        text = store_path.read_text(encoding='utf-8').strip()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            existing_key = (rec.get('name', ''), rec.get('source_card_oracle_id', ''))
            if existing_key == dedup_key:
                return  # Already persisted -- idempotent no-op

    # --- Write the new record --------------------------------------------
    resolved_dir.mkdir(parents=True, exist_ok=True)
    record = _attr_to_record(attr)
    line = json.dumps(record, sort_keys=True, ensure_ascii=False)
    with store_path.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


# ---------------------------------------------------------------------------
# Compounding read path -- assembles curated + machine for the walk
# ---------------------------------------------------------------------------

def all_edge_queries(store: ProvisionalAttributeStore | None = None) -> list[tuple[str, str]]:
    """Assemble ``(name, query)`` pairs from curated edges + machine store.

    This is the helper the engine derivation (PC) calls to build the full set
    of authored edge queries for the walk.  The curated edge queries come from
    :func:`.engine_hydration.curated_edge_queries`; machine-store queries come
    from the provided store (or an empty store if ``None``).

    Returns
    -------
    list[tuple[str, str]]
        Curated edges first, then machine edges, preserving insertion order.
    """
    from pipeline.transforms.strategy.engine_hydration import curated_edge_queries

    curated = curated_edge_queries()
    if store is None or store.is_empty:
        return curated
    return curated + store.edge_queries()
