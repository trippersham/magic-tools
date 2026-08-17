"""Real-resolver fixture-lake test for :func:`pipeline.sim.classify.classify_deck`.

The other classify tests use a MOCK resolver returning hand-picked
``otag_buckets``, so they never exercise the seam that actually ships: raw otag
SLUGS in the lake -> :func:`pipeline.transforms.crosswalk.buckets_for` -> the
bucket names ``classify_deck`` keys on. A crosswalk rename (``counterspell`` ->
``counterspells``) or a bucket-membership change would pass every mock test yet
silently break the live metric. This closes that gap: it seeds a tiny REAL lake
(``raw/oracle_cards`` + ``normalized/card_otag``) and classifies through the real
:class:`~pipeline.collection.resolver.DuckDBCardResolver` + real crosswalk.

Offline: a ``_BoomClient`` makes any network access an assertion failure, so a
lake hit is proven.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pipeline import store
from pipeline.collection.resolver import DuckDBCardResolver
from pipeline.sim.classify import classify_deck

# --------------------------------------------------------------------------- #
# A tiny lake whose otag SLUGS drive each crosswalk seam classify_deck depends on.
# --------------------------------------------------------------------------- #


def _oracle(name: str, *, oid: str, cmc: float, type_line: str) -> dict[str, Any]:
    return {
        'oracle_id': oid,
        'name': name,
        'cmc': cmc,
        'mana_cost': '',
        'type_line': type_line,
        'colors': [],
        'color_identity': [],
        'produced_mana': [],
        'keywords': [],
        'oracle_text': '',
        'power': None,
        'toughness': None,
        'art_crop': 'https://img/x.jpg',
        'scryfall_uri': 'https://scryfall.com/x',
        'set_name': 'Test',
    }


_ORACLE_CARDS: list[dict[str, Any]] = [
    _oracle('Counterspell', oid='counterspell-oid', cmc=2.0, type_line='Instant'),
    _oracle('Hardened Scales', oid='hardened-scales-oid', cmc=1.0, type_line='Enchantment'),
    _oracle('Doom Blade', oid='doom-blade-oid', cmc=2.0, type_line='Instant'),
    _oracle('Lightning Bolt', oid='lightning-bolt-oid', cmc=1.0, type_line='Instant'),
    _oracle('Sign in Blood', oid='sign-in-blood-oid', cmc=2.0, type_line='Sorcery'),
    _oracle('Command Tower', oid='command-tower-oid', cmc=0.0, type_line='Land'),
    _oracle('Grizzly Bears', oid='grizzly-bears-oid', cmc=2.0, type_line='Creature — Bear'),
]

# Raw rolled-up slugs (the shape card_otag stores). Each row drives a crosswalk
# bucket via buckets_for:
#   counterspell         -> 'counterspells' (the hard-counter seam)
#   counter-increaser    -> 'counters' (+1/+1 counters — the TRAP: NOT countermagic)
#   removal              -> 'removal'
#   burn                 -> 'burn' (Lightning Bolt ALSO carries removal -> real removal)
#   card-advantage       -> 'draw' (Sign in Blood: burn+draw, NO removal -> not removal, M2)
_CARD_OTAG: list[dict[str, str]] = [
    {'oracle_id': 'counterspell-oid', 'slug': 'counterspell'},
    {'oracle_id': 'hardened-scales-oid', 'slug': 'counter-increaser'},
    {'oracle_id': 'doom-blade-oid', 'slug': 'removal'},
    {'oracle_id': 'lightning-bolt-oid', 'slug': 'burn'},
    {'oracle_id': 'lightning-bolt-oid', 'slug': 'removal'},
    {'oracle_id': 'sign-in-blood-oid', 'slug': 'burn'},
    {'oracle_id': 'sign-in-blood-oid', 'slug': 'card-advantage'},
    # Grizzly Bears: present in the bulk, absent from card_otag -> empty buckets.
]


def _write_layer(payload: list[dict[str, Any]], layer: str, name: str) -> None:
    with store.connect() as conn:
        layer_dir = store.StorePaths.resolve().layer_dir(layer, create=True)
        tmp = layer_dir / f'_{name}.tmp.json'
        tmp.write_text(json.dumps(payload), encoding='utf-8')
        try:
            rel = conn.read_json(str(tmp))
            store.write_parquet(conn, rel, layer, name)
        finally:
            tmp.unlink(missing_ok=True)


class _BoomClient:
    def get(self, *_a: object, **_k: object) -> object:
        raise AssertionError('network access attempted on an offline lake hit')

    def close(self) -> None:  # pragma: no cover - trivial
        pass


@pytest.fixture()
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    _write_layer(_ORACLE_CARDS, 'raw', 'oracle_cards')
    _write_layer(_CARD_OTAG, 'normalized', 'card_otag')
    return root


def _classify(names: list[str]) -> object:
    # A real DuckDB resolver over the seeded lake; network access is fatal.
    return classify_deck(names, DuckDBCardResolver(client=_BoomClient()))


_DECK = ['Counterspell', 'Hardened Scales', 'Doom Blade', 'Lightning Bolt', 'Sign in Blood', 'Command Tower']


def test_available_through_real_lake(lake: Path) -> None:
    assert _classify(_DECK).available is True


def test_counterspell_slug_maps_to_counters_bucket(lake: Path) -> None:
    # The 'counterspell' slug -> 'counterspells' bucket -> counters set. This is the
    # exact crosswalk-slug<->classify-constant seam the mock tests cannot pin.
    c = _classify(_DECK)
    assert c.counters == frozenset({'Counterspell'})


def test_plus_one_counters_not_read_as_countermagic(lake: Path) -> None:
    # 'counter-increaser' -> 'counters' bucket (+1/+1), NOT 'counterspells'. Through
    # the real crosswalk, Hardened Scales must stay out of the counters set.
    assert 'Hardened Scales' not in _classify(_DECK).counters


def test_removal_bucket_is_removal_burn_only_is_not(lake: Path) -> None:
    # Doom Blade ('removal') and Lightning Bolt ('burn'+'removal') are removal;
    # Sign in Blood ('burn'+'card-advantage' -> buckets burn+draw, no removal) is
    # NOT (M2), proven through the real crosswalk this time.
    c = _classify(_DECK)
    assert c.removal == frozenset({'Doom Blade', 'Lightning Bolt'})
    assert 'Sign in Blood' not in c.removal


def test_costs_and_interaction_and_lands(lake: Path) -> None:
    c = _classify(_DECK)
    assert c.interaction == frozenset({'Counterspell', 'Doom Blade', 'Lightning Bolt'})
    assert c.costs == {'Counterspell': 2, 'Doom Blade': 2, 'Lightning Bolt': 1}
    assert c.lands == frozenset({'Command Tower'})


def test_deck_with_only_tagless_cards_is_unavailable(lake: Path) -> None:
    # Grizzly Bears is in the bulk but has NO otag rows -> no bucket resolves ->
    # UNKNOWN classification (available=False), NOT a fabricated 0/0. Because the
    # card DID resolve (just carries no otags), the reason is the otag-build cause,
    # not the unresolved-names one — pinned through the REAL resolver, not a mock.
    c = _classify(['Grizzly Bears'])
    assert c.available is False
    assert c.reason is not None
    assert 'otag build' in c.reason  # _NO_OTAGS_REASON (cards resolved, no otags)
