"""WS3 layer-1 deterministic catalog-integrity guards (validators / linters).

These are **validation gates**, not engine logic: every function here READS the
frozen catalog (``SEED_MAP``, the distill identity vocabulary, the
``card_attributes`` membership map) + the otag taxonomy and RETURNS a list of
violations.  Nothing here mutates the catalog or participates in engine output,
so adding this module is byte-parity (the three P0 SHAs are unchanged).  See
``ws3-integrity-system-design.md`` layer 1 and ADR 0009 / 0022 / 0003.

Two guards:

**Guard 1 — dead-ref validator (SEED_MAP + distill query atoms).**
    ``validate_seed_map`` (in :mod:`.derive_card_events`) already enforces the
    SEED_MAP seeds resolve to a non-empty closure.  :func:`validate_distill_atoms`
    extends dead-ref coverage to the *distill* layer: every ``otag:`` atom in a
    ``BASE_IDENTITIES`` / ``EDHREC_IDENTITIES`` / ``NEWBUCKET_IDENTITIES`` identity
    query, and every ``ARCHETYPE_EDGES`` key, must resolve to a **live otag, a
    registered synth attribute, or a defined (surfaced) attribute name**.
    :func:`validate_membership_atoms` does the same for the ``SYNTH_*`` and
    ``card_attributes`` queries (matched against real cards, so their ``otag:``
    atoms must be live otags / surfaced slugs).

    The worked dead ref the guard targets (A.1 finding): the old
    ``alt-wincon/combo`` identity's ``otag:alternate-win-condition`` disjunct —
    the distiller matches an identity query against a *pseudo-card whose only
    otag is the attribute NAME under test*, so ``otag:X`` can only ever fire for
    an attribute actually named ``X``.  A slug that is neither a live otag nor a
    name any card surfaces is a dead ref — it reads as if it does something it
    does not.

**Guard 2 — role-consistency linter.**
    :func:`validate_role_consistency` flags the synergy-proliferate-style
    inversion class: a **payoff-family otag used as a PRODUCER seed**
    (``produced_by``).  Payoff family = the "cares-about" naming families
    (``synergy-*`` / ``*-matters`` / ``*-fuel-*`` / ``*-cares``).  This is a
    BLOCKING check (a payoff seeded as a producer is a clear inversion).  It also
    reports — advisory only — role ambiguity: an otag used as a ``produced_by``
    seed in one event AND a ``triggers`` seed in another.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache

from pipeline.transforms.strategy.assets import (
    EDHREC_ATTR_SLUGS,
    ENCH_ATTR_SLUGS,
    VOCAB_FILE,
    StrategyAssets,
    load_assets,
)
from pipeline.transforms.strategy.derive_card_events import (
    SEED_MAP,
    load_taxonomy,
    validate_seed_map,
)
from pipeline.transforms.strategy.distill_strategies import (
    ARCHETYPE_EDGES,
    BASE_IDENTITIES,
    EDHREC_IDENTITIES,
    NEWBUCKET_IDENTITIES,
    SYNTH_ALWAYS_ATTRIBUTES,
    SYNTH_EDHREC_ATTRIBUTES,
)
from pipeline.transforms.strategy.query import _tokenize

# ---------------------------------------------------------------------------
# Atom extraction (reuses the ONE query tokenizer — no second parser)
# ---------------------------------------------------------------------------


def query_atoms(query: str) -> list[tuple[str, str]]:
    """Every ``(field, value)`` atom in *query* (``otag`` / ``oracle`` / ...).

    Reuses :func:`.query._tokenize` so atom extraction can never diverge from
    the evaluator's own parse.  ``devotion`` surfaces as ``('devotion', '')``.
    """
    atoms: list[tuple[str, str]] = []
    for kind, text in _tokenize(query):
        if kind == 'atom':
            field, _, value = text.partition(':')
            atoms.append((field, value))
    return atoms


def otag_atoms(query: str) -> list[str]:
    """The ``otag:<slug>`` values in *query*."""
    return [v for f, v in query_atoms(query) if f == 'otag']


# ---------------------------------------------------------------------------
# Resolution universe — what an atom is allowed to resolve to
# ---------------------------------------------------------------------------


def surfaced_attribute_names(assets: StrategyAssets) -> frozenset[str]:
    """Every attribute NAME a card can carry (the distill pseudo-card substrate).

    A card surfaces an attribute iff the name comes from one of the facts-layer
    surfacing paths (:func:`.derive_card_attributes.derive_card_attributes` +
    :func:`.distill_strategies.synth_attrs_for`): a ``card_attributes`` query
    name, a synth-attribute name, an event anchor, or an enchantment / EDHREC
    membership slug.  This is the set the distiller's ``otag:X`` pseudo-card test
    can ever match.
    """
    names: set[str] = set()
    names.update(name for name, _ in assets.card_attr_preds)
    names.update(name for name, _ in assets.synth_predicates)
    names.update(assets.event_anchors)
    names.update(ENCH_ATTR_SLUGS)
    names.update(EDHREC_ATTR_SLUGS)
    return frozenset(names)


@lru_cache(maxsize=1)
def resolution_universe() -> frozenset[str]:
    """``live_otags | surfaced_attribute_names`` — the dead-ref resolve target.

    An ``otag:X`` atom resolves iff ``X`` is a live otag (taxonomy slug) OR a
    name some card surfaces as an attribute (which also covers registered synth
    attrs and defined ``card_attributes`` names).  A live-but-unsurfaced otag is
    *inert belt-and-suspenders* in a distill query (it can never be distilled),
    not a dead ref — matching the vocabulary's own design note; a slug that is
    NEITHER live NOR surfaced is the dead ref this guard catches.

    Requires the lake (``load_taxonomy``); callers gate on lake availability.
    """
    assets = load_assets()
    return frozenset(set(load_taxonomy()) | surfaced_attribute_names(assets))


# ---------------------------------------------------------------------------
# Guard 1 — dead-ref validator (distill-layer extension of validate_seed_map)
# ---------------------------------------------------------------------------


def validate_distill_atoms(
    identities: list[tuple[str, str]] | None = None,
    edges: dict[str, str] | None = None,
    *,
    resolve: frozenset[str] | None = None,
) -> list[str]:
    """Dead-ref check over the distill identity layer.  Returns violations.

    For every identity ``(name, query)``:

    * each ``otag:X`` atom must have ``X`` in *resolve* (a live otag / synth
      attr / surfaced attribute name) — else it is a dead ref;
    * a non-``otag`` atom (``oracle:`` / ``type:`` / ``keyword:`` / ``devotion``)
      can never match a distill pseudo-card (whose only field is a single otag),
      so it is a dead atom at the distill layer.

    Every ``ARCHETYPE_EDGES`` key must be a defined identity name (an edge whose
    identity does not exist is a dead reference the distiller silently drops).

    Parameters are injectable for hermetic testing; the defaults are the frozen
    catalog + the lake-backed :func:`resolution_universe`.
    """
    if identities is None:
        identities = EDHREC_IDENTITIES + NEWBUCKET_IDENTITIES + BASE_IDENTITIES
    if edges is None:
        edges = ARCHETYPE_EDGES
    if resolve is None:
        resolve = resolution_universe()

    violations: list[str] = []
    identity_names = {name for name, _ in identities}

    for name, query in identities:
        for field, value in query_atoms(query):
            if field != 'otag':
                violations.append(
                    f'distill identity {name!r}: {field}:{value!r} is a non-otag atom — '
                    f'a distill pseudo-card carries only an otag, so this can never match (dead atom)'
                )
            elif value not in resolve:
                violations.append(
                    f'distill identity {name!r}: otag {value!r} resolves to no live otag / '
                    f'synth attr / surfaced attribute (dead ref)'
                )

    for key in edges:
        if key not in identity_names:
            violations.append(f'ARCHETYPE_EDGES key {key!r} is not a defined identity (dead edge)')

    return violations


def _membership_queries() -> list[tuple[str, str]]:
    """The ``(name, query)`` pairs matched against REAL cards.

    These are the synth-attribute queries and the ``card_attributes`` membership
    queries — matched against a real card view, so their ``otag:`` atoms must be
    real (live / surfaced) otags.  ``None``-valued ``card_attributes`` entries
    (declared-but-not-derivable) are skipped by their callers; here an empty
    query contributes no atoms.
    """
    vocab = json.loads(VOCAB_FILE.read_text())
    pairs: list[tuple[str, str]] = []
    pairs += SYNTH_EDHREC_ATTRIBUTES
    pairs += SYNTH_ALWAYS_ATTRIBUTES
    pairs += [(name, q) for name, q in vocab['card_attributes'].items() if q]
    return pairs


def validate_membership_atoms(
    pairs: list[tuple[str, str]] | None = None,
    *,
    resolve: frozenset[str] | None = None,
) -> list[str]:
    """Dead-ref check over the real-card membership queries.  Returns violations.

    Every ``otag:X`` atom in a ``SYNTH_*`` / ``card_attributes`` query must
    resolve (``X`` a live otag / surfaced slug).  Field atoms (``oracle:`` /
    ``type:`` / ``keyword:`` / ``devotion``) test card fields and always resolve.
    """
    if pairs is None:
        pairs = _membership_queries()
    if resolve is None:
        resolve = resolution_universe()

    violations: list[str] = []
    for name, query in pairs:
        for value in otag_atoms(query):
            if value not in resolve:
                violations.append(
                    f'membership query {name!r}: otag {value!r} resolves to no live/surfaced otag (dead ref)'
                )
    return violations


# ---------------------------------------------------------------------------
# Guard 2 — role-consistency linter (payoff-as-producer + role ambiguity)
# ---------------------------------------------------------------------------

#: The "cares-about" payoff naming families.  Verified against the taxonomy
#: (``synergy-*`` 277, ``*-matters`` 92, ``*-fuel-*`` 35, ``*-cares`` 0 — the
#: cares-about family is realized as ``*-matters`` in this taxonomy; the
#: ``*-cares`` pattern is kept for forward-compat as new sets add otags).
PAYOFF_FAMILY_PATTERNS: tuple[str, ...] = (r'^synergy-', r'-matters$', r'-fuel-', r'-cares$')
_PAYOFF_FAMILY_RE: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in PAYOFF_FAMILY_PATTERNS)


def is_payoff_family(slug: str) -> bool:
    """True if *slug* is a payoff-family (cares-about) otag name.

    A payoff (a card that CARES about a mechanic) must never be a ``produced_by``
    seed — producers are the cards that DO the mechanic.
    """
    return any(rx.search(slug) for rx in _PAYOFF_FAMILY_RE)


#: Scryfall-taxonomy BOOKKEEPING families — labels that catalogue a card's
#: provenance or identity rather than what it DOES.  Verified against the live
#: taxonomy (``cycle-*`` 1677, ``typal-*`` 254, ``type-*`` 49, ``unique-*`` 19):
#:   * ``cycle-*``  — "printed together as a themed group in one set" (a
#:     publishing fact; e.g. ``cycle-c18-planeswalker`` = four unrelated 2018
#:     commanders).
#:   * ``typal-*``  — tribal-theme membership.  Load-bearing for TRIBAL-ARCHETYPE
#:     detection in distill (``otag:typal``), but never a producer of a game
#:     event — skipped on the producer side ONLY.
#:   * ``type-*``   — type-line errata / additions (``type-errata-*`` / ``type-addition-*``).
#:   * ``unique-*`` — one-of-a-kind trivia (``unique-mana-symbol`` / ``unique-p-t`` …).
#: These are Scryfall's cataloguing labels (``raw/oracle_tags.parquet`` from
#: tagger.scryfall.com), not pipeline-authored.  A bookkeeping tag corroborates
#: at N>=3 by coincidence of set design or trivia, so it must never be proposed
#: as an event producer (owner-ratified 2026-11-18).
BOOKKEEPING_FAMILY_PATTERNS: tuple[str, ...] = (r'^cycle-', r'^typal-', r'^type-', r'^unique-')
_BOOKKEEPING_FAMILY_RE: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in BOOKKEEPING_FAMILY_PATTERNS)

#: Stray Scryfall bookkeeping/errata otags that fall OUTSIDE the ``^cycle-/
#: ^typal-/^type-/^unique-`` prefix families but are nonetheless cataloguing
#: facts, not mechanics a card DOES.  Owner-ratified 2026-11-18 (R5d producer
#: walkthrough) as an explicit skip set so future concept-anchored discovery
#: never resurfaces them as candidate producers:
#:   * ``unprinted-token``     — "tokens not yet printed in paper" (a paper-
#:     availability fact, not an in-game token-making ability).
#:   * ``token-errata``        — "used to make a different token type, since
#:     updated" (an errata note about a past printing).
#:   * ``no-planeswalker-type`` — planeswalker-identity trivia (a card that lacks
#:     a planeswalker type), not a creature/permanent-entering producer.
BOOKKEEPING_EXTRA_SLUGS: frozenset[str] = frozenset({'unprinted-token', 'token-errata', 'no-planeswalker-type'})


def is_bookkeeping_family(slug: str) -> bool:
    """True if *slug* is a Scryfall bookkeeping (provenance/identity) otag name.

    A bookkeeping tag describes where a card was printed or a one-of-a-kind trait,
    not what the card DOES, so it must never be proposed as an event producer.
    ``typal-*`` is skipped on the producer side here but remains authoritative for
    tribal-archetype detection in :mod:`.distill_strategies` (a different layer).

    Covers the four prefix families (``^cycle-/^typal-/^type-/^unique-``) AND the
    explicit :data:`BOOKKEEPING_EXTRA_SLUGS` stragglers (owner-ratified) so a
    future discovery run never resurfaces the stray errata/availability tags.
    """
    return slug in BOOKKEEPING_EXTRA_SLUGS or any(rx.search(slug) for rx in _BOOKKEEPING_FAMILY_RE)


@dataclass(frozen=True)
class RoleReport:
    """Outcome of :func:`validate_role_consistency`.

    ``payoff_producers`` is BLOCKING (a payoff seeded as a producer is a clear
    inversion); ``role_ambiguous`` is advisory (worth owner review, not a block).
    """

    payoff_producers: tuple[str, ...]
    role_ambiguous: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """True when the BLOCKING check is clean (advisory ambiguity ignored)."""
        return not self.payoff_producers


def validate_role_consistency(seed_map: dict[str, dict] | None = None) -> RoleReport:
    """Flag payoff-family otags used as PRODUCER seeds (+ advisory role ambiguity).

    * **payoff_producers (BLOCKING):** any ``produced_by`` seed whose otag is
      payoff-family (:func:`is_payoff_family`) — "a payoff is seeded as a
      producer" (the ``synergy-proliferate`` inversion class).
    * **role_ambiguous (advisory):** an otag used as a ``produced_by`` seed in
      one event AND a ``triggers`` seed in another — a role the catalog uses both
      ways; worth review, not a block (producer/consumer duals like ``flicker``
      are legitimate).
    """
    if seed_map is None:
        seed_map = SEED_MAP

    producer_of: dict[str, list[str]] = {}
    trigger_of: dict[str, list[str]] = {}
    payoff_producers: list[str] = []

    for event, entry in seed_map.items():
        for seed in entry['produced_by']['seeds']:
            producer_of.setdefault(seed, []).append(event)
            if is_payoff_family(seed):
                payoff_producers.append(
                    f'{event}.produced_by: payoff-family otag {seed!r} seeded as a PRODUCER '
                    f'(a payoff is not a producer — role inversion)'
                )
        for seed in entry['triggers']['seeds']:
            trigger_of.setdefault(seed, []).append(event)

    role_ambiguous: list[str] = []
    for seed in sorted(set(producer_of) & set(trigger_of)):
        role_ambiguous.append(
            f'{seed!r}: produced_by seed of {sorted(producer_of[seed])} AND '
            f'triggers seed of {sorted(trigger_of[seed])} (role ambiguity — review)'
        )

    return RoleReport(payoff_producers=tuple(payoff_producers), role_ambiguous=tuple(role_ambiguous))


# ---------------------------------------------------------------------------
# Umbrella — the full blocking gate (SEED_MAP + distill + membership + role)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogReport:
    """Aggregate catalog-integrity report.

    ``blocking`` is the union of the BLOCKING checks (SEED_MAP dead refs, distill
    dead refs, membership dead refs, payoff-as-producer inversions).
    ``advisory`` is report-only (role ambiguity).
    """

    seed_map: tuple[str, ...]
    distill_atoms: tuple[str, ...]
    membership_atoms: tuple[str, ...]
    payoff_producers: tuple[str, ...]
    role_ambiguous: tuple[str, ...]

    @property
    def blocking(self) -> tuple[str, ...]:
        return self.seed_map + self.distill_atoms + self.membership_atoms + self.payoff_producers

    @property
    def advisory(self) -> tuple[str, ...]:
        return self.role_ambiguous

    @property
    def ok(self) -> bool:
        return not self.blocking


def validate_catalog() -> CatalogReport:
    """Run every catalog-integrity guard over the frozen catalog + live lake.

    Requires the lake (taxonomy); callers gate on lake availability.
    """
    role = validate_role_consistency()
    return CatalogReport(
        seed_map=tuple(validate_seed_map()),
        distill_atoms=tuple(validate_distill_atoms()),
        membership_atoms=tuple(validate_membership_atoms()),
        payoff_producers=role.payoff_producers,
        role_ambiguous=role.role_ambiguous,
    )
