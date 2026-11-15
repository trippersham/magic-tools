"""Engine cache -- the materialized role-B (L5) synthesis store.

Role-B resolves each deck's deterministic strategy-graph outputs (elected engine
candidates + detected combos + commander + archetype identity) into the ONE core
Engine ``{strategy, win_condition, key_pieces, confidence}``.  Unlike Tier-1
(the deterministic ``resolve(deck)`` archetype identity, always available), the
Engine is a **Tier-2 enrichment** synthesised by a language model.  Synthesis is
expensive, so this module materialises each deck's resolved Engine to a durable
JSONL cache keyed by a stable staleness key, exactly like the combo-edge /
machine-vocab stores.

Staleness key (ADR 0021 two-layer model, extended for L5)
--------------------------------------------------------
A cached Engine is **fresh** for a deck iff BOTH hold:

* ``input_hash`` matches -- the role-B *input* (the deterministic output of
  ``compute_facts`` + ``name_deck`` on the deck) is byte-identical.  Because the
  input carries the full elected set, detected combos, and archetype identity,
  ANY engine-logic change (B1 combo-edges, B6 archetype-gated election, a new
  curated edge, a deck edit) flows through to a different ``input_hash`` and so
  invalidates the cache automatically.  This is the "deck or engine-logic
  changed" trigger, self-maintaining -- no manual version bump needed for engine
  changes.
* ``synthesis_version`` matches -- bumped by hand only when the synthesis
  *prompt / rules* change (the input is unchanged but we want a re-run).

On-disk format
--------------
JSONL at ``<store_dir>/engines.jsonl`` (one JSON object per line), the same
human-inspectable, grep-able, append-only shape as the machine-vocab store.
``store_dir`` defaults to ``~/.local/share/make-magic/engine_cache/`` (the XDG
data dir).  This is a **provisional / Tier-2** artifact -- it is never on the
frozen P0 deterministic path and curated-base tests never consult it.

Idempotency
-----------
``persist`` / ``persist_batch`` deduplicate on ``slug`` keeping the newest
record (last write wins), so a resumed materialisation run is a no-op for decks
already cached fresh and an in-place refresh for decks whose input changed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pipeline.transforms.strategy.machine_vocab import (
    DERIVATION_VERSION,
    EXTRACTION_VERSION,
)

# ---------------------------------------------------------------------------
# Version constant (L5 synthesis layer)
# ---------------------------------------------------------------------------

#: Bump when the role-B synthesis *prompt / rules* change (``roleb_prompt.md``).
#: An engine-logic change does NOT need a bump here -- it flows through the
#: ``input_hash`` automatically.  This only covers "input unchanged, but the
#: synthesiser should run again because the rules changed".
#:
#: History:
#:   v1 -- initial materialization prompt (graph-outputs only).
#:   v2 -- add ``in_deck_cards`` roster + strengthen R5 so ``key_pieces`` are
#:         grounded in the deck's real cards (enforceable R5 in-deck check).
SYNTHESIS_VERSION: int = 2


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedEngine:
    """The ONE core Engine resolved for a deck (role-B output).

    This is the strict output shape the synthesiser must produce.  ``key_pieces``
    is a tuple (hashable, immutable) of in-deck card names.
    """

    strategy: str
    win_condition: str
    key_pieces: tuple[str, ...]
    confidence: str  # 'HIGH' | 'MED' | 'LOW'

    def as_dict(self) -> dict:
        return {
            'strategy': self.strategy,
            'win_condition': self.win_condition,
            'key_pieces': list(self.key_pieces),
            'confidence': self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ResolvedEngine:
        return cls(
            strategy=d.get('strategy', ''),
            win_condition=d.get('win_condition', ''),
            key_pieces=tuple(d.get('key_pieces', []) or []),
            confidence=d.get('confidence', 'LOW'),
        )


@dataclass(frozen=True)
class EngineCacheRecord:
    """A single durable cache record: a deck's resolved Engine + provenance."""

    slug: str
    deck_name: str
    input_hash: str
    synthesis_version: int
    engine: ResolvedEngine
    #: The model that authored the Engine (e.g. ``'claude-sonnet-4-5'``).
    model_id: str = ''
    #: ISO-8601 UTC timestamp of synthesis.
    synthesized_at: str = ''
    #: Carried for traceability / cross-store alignment (ADR 0021).  Not part of
    #: the staleness key -- those live in the input_hash, but stamping them lets
    #: an operator correlate an Engine with the vocab generation it saw.
    extraction_version: int = EXTRACTION_VERSION
    derivation_version: int = DERIVATION_VERSION

    def to_record(self) -> dict:
        rec = {
            'slug': self.slug,
            'deck_name': self.deck_name,
            'input_hash': self.input_hash,
            'synthesis_version': self.synthesis_version,
            'engine': self.engine.as_dict(),
            'model_id': self.model_id,
            'synthesized_at': self.synthesized_at,
            'extraction_version': self.extraction_version,
            'derivation_version': self.derivation_version,
        }
        return rec

    @classmethod
    def from_record(cls, rec: dict) -> EngineCacheRecord:
        return cls(
            slug=rec['slug'],
            deck_name=rec.get('deck_name', rec['slug']),
            input_hash=rec.get('input_hash', ''),
            synthesis_version=rec.get('synthesis_version', 0),
            engine=ResolvedEngine.from_dict(rec.get('engine', {})),
            model_id=rec.get('model_id', ''),
            synthesized_at=rec.get('synthesized_at', ''),
            extraction_version=rec.get('extraction_version', 0),
            derivation_version=rec.get('derivation_version', 0),
        )


# ---------------------------------------------------------------------------
# Staleness key
# ---------------------------------------------------------------------------

def input_hash(roleb_input: dict) -> str:
    """Stable SHA-256 over the canonical role-B input.

    Internal ``_``-prefixed keys (``_sparse``, ``_failed``, ``_error`` ...) and
    the free-text ``deck_name`` are excluded so cosmetic changes don't bust the
    cache; everything that actually drives synthesis (commanders, elected set,
    archetype identity, detected combos, commander loop, naming resolution,
    counts) is hashed.  Serialisation is canonical (sorted keys, no whitespace)
    so the hash is reproducible across machines and runs.
    """
    payload = {
        k: v
        for k, v in roleb_input.items()
        if not k.startswith('_') and k != 'deck_name'
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

@dataclass
class EngineCache:
    """In-memory view of the durable engine cache (keyed by slug)."""

    records: dict[str, EngineCacheRecord] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.records

    def get(self, slug: str) -> EngineCacheRecord | None:
        return self.records.get(slug)

    def is_fresh(
        self,
        slug: str,
        input_hash_value: str,
        *,
        synthesis_version: int = SYNTHESIS_VERSION,
    ) -> bool:
        """True iff a cached record for ``slug`` matches the staleness key.

        Fresh == same ``input_hash`` AND ``synthesis_version >=`` the requested
        version.  A record stamped with an older synthesis version, or whose
        input has changed, is stale and must be re-synthesised.
        """
        rec = self.records.get(slug)
        if rec is None:
            return False
        return (
            rec.input_hash == input_hash_value
            and rec.synthesis_version >= synthesis_version
        )


#: Default store location (XDG data home / make-magic).
_DEFAULT_STORE_DIR = Path.home() / '.local' / 'share' / 'make-magic' / 'engine_cache'

#: Filename within the store directory.
_STORE_FILENAME = 'engines.jsonl'


def _resolve_store_path(store_dir: Path | str | None) -> Path:
    if store_dir is not None:
        store_dir = Path(store_dir)
    return (store_dir or _DEFAULT_STORE_DIR) / _STORE_FILENAME


def empty_cache() -> EngineCache:
    """Return a guaranteed-empty cache (the identity element)."""
    return EngineCache()


def load_cache(store_dir: Path | str | None = None) -> EngineCache:
    """Load the durable engine cache from disk.

    Reads every line of ``<store_dir>/engines.jsonl``.  Later lines for the same
    ``slug`` win (append-only refresh semantics).  Returns an empty cache if the
    file is missing or empty.
    """
    store_path = _resolve_store_path(store_dir)
    if not store_path.exists():
        return EngineCache()

    text = store_path.read_text(encoding='utf-8').strip()
    if not text:
        return EngineCache()

    records: dict[str, EngineCacheRecord] = {}
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            rec = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f'{store_path}:{line_no}: invalid JSON: {exc}') from exc
        record = EngineCacheRecord.from_record(rec)
        records[record.slug] = record  # last write wins

    return EngineCache(records=records)


def persist(
    record: EngineCacheRecord,
    store_dir: Path | str | None = None,
) -> None:
    """Append a single cache record to the durable store.

    **Idempotency:** if the store already holds a record for ``record.slug``
    with the same ``input_hash`` and a ``synthesis_version >=`` this record's,
    the call is a silent no-op (nothing is appended).  Otherwise the new record
    is appended; on load the newest line for a slug wins, so this is an in-place
    refresh.
    """
    store_path = _resolve_store_path(store_dir)
    resolved_dir = store_path.parent

    if store_path.exists():
        existing = load_cache(store_dir)
        prev = existing.get(record.slug)
        if (
            prev is not None
            and prev.input_hash == record.input_hash
            and prev.synthesis_version >= record.synthesis_version
        ):
            return  # already fresh -- idempotent no-op

    resolved_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.to_record(), sort_keys=True, ensure_ascii=False)
    with store_path.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def persist_batch(
    records: list[EngineCacheRecord],
    store_dir: Path | str | None = None,
) -> int:
    """Append many records in one pass, skipping those already fresh.

    Returns the number of records actually written.
    """
    if not records:
        return 0

    store_path = _resolve_store_path(store_dir)
    resolved_dir = store_path.parent
    existing = load_cache(store_dir)

    to_write: list[EngineCacheRecord] = []
    seen: dict[str, EngineCacheRecord] = dict(existing.records)
    for record in records:
        prev = seen.get(record.slug)
        if (
            prev is not None
            and prev.input_hash == record.input_hash
            and prev.synthesis_version >= record.synthesis_version
        ):
            continue
        to_write.append(record)
        seen[record.slug] = record

    if not to_write:
        return 0

    resolved_dir.mkdir(parents=True, exist_ok=True)
    with store_path.open('a', encoding='utf-8') as f:
        for record in to_write:
            f.write(json.dumps(record.to_record(), sort_keys=True, ensure_ascii=False) + '\n')

    return len(to_write)


def make_record(
    slug: str,
    deck_name: str,
    roleb_input: dict,
    engine: ResolvedEngine,
    *,
    model_id: str = '',
    synthesis_version: int = SYNTHESIS_VERSION,
) -> EngineCacheRecord:
    """Assemble a cache record, stamping the staleness key + provenance."""
    return EngineCacheRecord(
        slug=slug,
        deck_name=deck_name,
        input_hash=input_hash(roleb_input),
        synthesis_version=synthesis_version,
        engine=engine,
        model_id=model_id,
        synthesized_at=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    )


# Re-export asdict for callers that want the raw record dict without a dataclass.
__all__ = [
    'SYNTHESIS_VERSION',
    'ResolvedEngine',
    'EngineCacheRecord',
    'EngineCache',
    'input_hash',
    'empty_cache',
    'load_cache',
    'persist',
    'persist_batch',
    'make_record',
    'asdict',
]
