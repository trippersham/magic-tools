"""Deck -> interaction classification for the piloting metric (otag-bucket driven).

:func:`~pipeline.sim.telemetry.extract_piloting` needs to know, for the candidate
deck, WHICH cards are counters / removal / interaction, each card's mana value
(the affordability proxy), and which names are lands. This module derives that
classification from each card's crosswalk ``otag_buckets`` (the same functional
buckets ``card_tagger`` reads), so it is engine-agnostic — XMage will reuse it.

**Honesty is the hard requirement.** The otag lake may be EMPTY (a dev env
"serving live only" returns ``otag_buckets=[]`` for every card). If NO card in
the deck resolves ANY otag bucket, the classification is UNKNOWN — we CANNOT emit
a ``0/0`` "no counters / AI piloted fine" reading (the building-decks m8 lesson).
:class:`Classification` then carries ``available=False`` + a ``reason``, and the
caller surfaces an honest "unavailable" line instead of fabricated zeros. This is
DISTINCT from a deck that genuinely runs no interaction: there SOME card resolved
a bucket (the lake is populated), the counter/removal sets are just empty, and the
resulting real zeros ARE meaningful (``available=True``).

The bucket mapping (crosswalk slugs, see :mod:`pipeline.transforms.crosswalk`):

  * **counters** — buckets include ``'counterspells'``. NOT ``'counters'`` — that
    bucket is +1/+1 counters, an unrelated mechanic; conflating them would count
    every counter-matters creature as countermagic.
  * **removal** — buckets include ``'removal'``. NOT ``'burn'``: the crosswalk's
    ``burn`` bucket folds direct damage together with life-loss / drain
    (``opponent-loses-life`` / ``drain-life``) for deck-balance counting, so it
    mis-classes life-loss cards that can't kill a creature (Sign in Blood,
    Exsanguinate) as removal. Real damage-based removal also carries ``'removal'``,
    so keying on it alone keeps burn removal while dropping the life-loss leak (M2).
  * **interaction** — ``counters | removal``.
  * **costs** — ``{name: int(mana_value)}`` for the interaction cards (the
    ``untapped_lands >= mv`` affordability proxy; a card with no ``mana_value`` is
    omitted so :func:`extract_piloting` treats it as unaffordable).
  * **lands** — cards whose ``type_line`` contains ``'Land'``, plus the five basics
    (always lands even when unresolved).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pipeline.collection.resolver import default_card_resolver

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pipeline.collection.store import CardResolver

__all__ = ('Classification', 'classify_deck')

#: The five basic land names — always lands, even when the resolver can't hydrate
#: them (a name-only card carries no ``type_line``).
_BASICS = frozenset({'Plains', 'Island', 'Swamp', 'Mountain', 'Forest'})

#: The reason attached to an UNAVAILABLE classification (empty otag lake).
_EMPTY_LAKE_REASON = 'otag lake not populated — run the otag build to enable piloting metrics'


@dataclass(frozen=True)
class Classification:
    """The interaction classification :func:`extract_piloting` consumes.

    ``available`` is False when the otag lake resolved NO buckets for ANY card in
    the deck (UNKNOWN classification): the caller must then NOT compute a profile
    (a ``0/0`` would falsely read as "no interaction / AI fine") and instead
    surface the honest ``reason``. When True, the sets are real — an empty
    ``counters`` genuinely means the deck runs no countermagic.
    """

    counters: frozenset[str]
    removal: frozenset[str]
    interaction: frozenset[str]
    costs: dict[str, int]
    lands: frozenset[str]
    available: bool
    reason: str | None = None

    def as_kwargs(self) -> dict[str, object]:
        """The keyword args :func:`~pipeline.sim.telemetry.extract_piloting` takes.

        Splat into the parser as ``**classification.as_kwargs()``. Only meaningful
        when :attr:`available` — an unavailable classification must be short-
        circuited by the caller before it ever reaches the parser.
        """
        return {
            'counters': self.counters,
            'removal': self.removal,
            'interaction': self.interaction,
            'costs': self.costs,
            'lands': self.lands,
        }


def _unavailable() -> Classification:
    return Classification(
        counters=frozenset(),
        removal=frozenset(),
        interaction=frozenset(),
        costs={},
        lands=frozenset(),
        available=False,
        reason=_EMPTY_LAKE_REASON,
    )


def classify_deck(card_names: Iterable[str], resolver: CardResolver | None = None) -> Classification:
    """Classify a deck's cards into the sets :func:`extract_piloting` needs.

    Resolves each name via ``resolver`` (default: the package
    :func:`~pipeline.collection.resolver.default_card_resolver`) and reads its
    ``otag_buckets`` / ``mana_value`` / ``type_line``. See the module docstring
    for the bucket mapping.

    SPARSE-LAKE HONESTY: if NO resolved card carries ANY ``otag_buckets`` (the
    lake is empty — "serving live only"), the classification is UNKNOWN and the
    returned :class:`Classification` has ``available=False`` + a ``reason``. Only
    when at least one card resolved a bucket is the result ``available`` (and then
    an empty ``counters`` is a REAL "no countermagic", not a missing-data
    artifact). Lands are still collected on either path (they need no otag), but
    are irrelevant when unavailable.
    """
    if resolver is None:
        resolver = default_card_resolver()

    # De-dupe up front (preserve order) so the iterable is materialized once — a
    # bare generator would otherwise be exhausted after the first pass.
    names = list(dict.fromkeys(card_names))

    counters: set[str] = set()
    removal: set[str] = set()
    costs: dict[str, int] = {}
    lands: set[str] = {name for name in names if name in _BASICS}

    any_bucket_resolved = False

    for name in names:
        card = resolver.get_card(name)
        if card is None:
            continue
        buckets = set(card.otag_buckets or ())
        if buckets:
            any_bucket_resolved = True
        type_line = card.type_line or ''
        if 'Land' in type_line:
            lands.add(name)

        is_counter = 'counterspells' in buckets  # NOT 'counters' (+1/+1 counters).
        # Removal = the `removal` bucket ONLY. NOT the `burn` bucket: that bucket
        # deliberately folds direct damage together with life-loss / drain
        # (`opponent-loses-life`, `drain-life`) for deck-balance counting, so
        # keying on it mis-classes life-loss cards that can't kill a creature —
        # Sign in Blood (buckets `burn`+`draw`), Exsanguinate (`burn`) — as
        # removal, inflating the piloting denominator (M2). Genuine damage-based
        # removal (Lightning Bolt, Sear, Bombard, sweepers) ALSO carries the
        # `removal` bucket, so `removal` alone keeps real burn removal while
        # dropping the life-loss leak.
        is_removal = 'removal' in buckets
        if is_counter:
            counters.add(name)
        if is_removal:
            removal.add(name)
        if (is_counter or is_removal) and card.mana_value is not None:
            costs[name] = int(card.mana_value)

    if not any_bucket_resolved:
        # UNKNOWN: the lake resolved no functional buckets for any card. Do NOT emit
        # a 0/0 profile — mark it unavailable so the caller prints an honest line.
        return _unavailable()

    interaction = counters | removal
    return Classification(
        counters=frozenset(counters),
        removal=frozenset(removal),
        interaction=frozenset(interaction),
        costs=costs,
        lands=frozenset(lands),
        available=True,
    )
