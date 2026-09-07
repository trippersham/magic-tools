"""Tests for the DuckDB-backed card-dim resolver.

The resolver satisfies the `CardResolver` port (`get_card(name) -> Card | None`)
by reading `raw/oracle_cards` from the lake (offline-first), joining the
`normalized/card_otag` rollup for `otags` / `otag_buckets`, and falling back to a
single live Scryfall fetch on a bulk miss (landed durably so the next lookup is
offline). Everything fails open: a missing lake or otag layer degrades, never
crashes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline import store
from pipeline.collection import resolver as resolver_mod
from pipeline.collection.resolver import DuckDBCardResolver, default_card_resolver
from pipeline.collection.store import CardResolver

# --------------------------------------------------------------------------- #
# Fixtures — seed a lake (raw/oracle_cards + normalized/card_otag) under a tmp
# data dir, so a lookup resolves OFFLINE.
# --------------------------------------------------------------------------- #

# A DFC / split card stores the FULL "Front // Back" name in oracle_cards.
_ORACLE_CARDS: list[dict[str, Any]] = [
    {
        'oracle_id': 'sol-ring-oid',
        'name': 'Sol Ring',
        'cmc': 1.0,
        'mana_cost': '{1}',
        'type_line': 'Artifact',
        'colors': [],
        'color_identity': [],
        'produced_mana': ['C'],
        'keywords': [],
        'oracle_text': '{T}: Add {C}{C}.',
        'power': None,
        'toughness': None,
        'art_crop': 'https://img/sol-ring.jpg',
        'image_normal': 'https://img/sol-ring-normal.jpg',
        'scryfall_uri': 'https://scryfall.com/card/c21/263/sol-ring',
        'set_name': 'Commander 2021',
    },
    {
        'oracle_id': 'llanowar-oid',
        'name': 'Llanowar Elves',
        'cmc': 1.0,
        'mana_cost': '{G}',
        'type_line': 'Creature — Elf Druid',
        'colors': ['G'],
        'color_identity': ['G'],
        'produced_mana': ['G'],
        'keywords': [],
        'oracle_text': '{T}: Add {G}.',
        'power': '1',
        'toughness': '1',
        'art_crop': 'https://img/llanowar.jpg',
        'image_normal': 'https://img/llanowar-normal.jpg',
        'scryfall_uri': 'https://scryfall.com/card/fdn/227/llanowar-elves',
        'set_name': 'Foundations',
    },
    {
        'oracle_id': 'fable-oid',
        # A DFC/split card: the full front // back name is what the lake stores.
        'name': 'Fable of the Mirror-Breaker // Reflection of Kiki-Jiki',
        'cmc': 3.0,
        'mana_cost': '{2}{R}',
        'type_line': 'Enchantment — Saga // Enchantment Creature — Goblin Shaman',
        'colors': ['R'],
        'color_identity': ['R'],
        'produced_mana': [],
        'keywords': [],
        'oracle_text': '(As this Saga enters…)',
        'power': None,
        'toughness': None,
        'art_crop': 'https://img/fable.jpg',
        'image_normal': 'https://img/fable-normal.jpg',
        'scryfall_uri': 'https://scryfall.com/card/neo/141/fable',
        'set_name': 'Kamigawa: Neon Dynasty',
    },
    {
        'oracle_id': 'split-oid',
        # A DFC/split/adventure card: the lake stores the full 'Front // Back' name.
        'name': 'Split Alpha // Split Beta',
        'cmc': 2.0,
        'mana_cost': '{1}{R}',
        'type_line': 'Instant // Instant',
        'colors': ['R'],
        'color_identity': ['R'],
        'produced_mana': [],
        'keywords': [],
        'oracle_text': '(front) // (back)',
        'power': None,
        'toughness': None,
        'art_crop': 'https://img/split.jpg',
        'image_normal': 'https://img/split-normal.jpg',
        'scryfall_uri': 'https://scryfall.com/x',
        'set_name': 'Test',
    },
    {
        'oracle_id': 'alpha-oid',
        # A DISTINCT plain card whose name collides with the DFC's FRONT face —
        # exact match must win over the face match (no shadowing).
        'name': 'Split Alpha',
        'cmc': 2.0,
        'mana_cost': '{U}{U}',
        'type_line': 'Instant',
        'colors': ['U'],
        'color_identity': ['U'],
        'produced_mana': [],
        'keywords': [],
        'oracle_text': 'Counter target spell.',
        'power': None,
        'toughness': None,
        'art_crop': 'https://img/alpha.jpg',
        'image_normal': 'https://img/alpha-normal.jpg',
        'scryfall_uri': 'https://scryfall.com/x',
        'set_name': 'Test',
    },
    {
        'oracle_id': 'jaya-oid',
        # A punctuation / apostrophe name.
        'name': "Jaya's Immolating Inferno",
        'cmc': 3.0,
        'mana_cost': '{X}{R}{R}{R}',
        'type_line': 'Sorcery',
        'colors': ['R'],
        'color_identity': ['R'],
        'produced_mana': [],
        'keywords': [],
        'oracle_text': "Jaya's Immolating Inferno deals X damage.",
        'power': None,
        'toughness': None,
        'art_crop': 'https://img/jaya.jpg',
        'image_normal': 'https://img/jaya-normal.jpg',
        'scryfall_uri': 'https://scryfall.com/card/dom/128/jayas-immolating-inferno',
        'set_name': 'Dominaria',
    },
]

# card_otag rollup rows (oracle_id, slug). Sol Ring rolls up to ramp slugs.
_CARD_OTAG: list[dict[str, str]] = [
    {'oracle_id': 'sol-ring-oid', 'slug': 'ramp'},
    {'oracle_id': 'sol-ring-oid', 'slug': 'mana-rock'},
    {'oracle_id': 'llanowar-oid', 'slug': 'ramp'},
    {'oracle_id': 'llanowar-oid', 'slug': 'mana-dork'},
    # The DFC 'Fable ...' + the split card carry otags keyed by the FULL-name oid,
    # so a FACE-name lookup must hit the full row to pick them up (the #49 fix).
    {'oracle_id': 'fable-oid', 'slug': 'token-increaser'},
    {'oracle_id': 'split-oid', 'slug': 'removal'},
    {'oracle_id': 'alpha-oid', 'slug': 'counterspell'},
]


def _write_layer(payload: list[dict[str, Any]], layer: str, name: str) -> None:
    """Materialize `payload` as `data/<layer>/<name>.parquet` via the store."""
    with store.connect() as conn:
        layer_dir = store.StorePaths.resolve().layer_dir(layer, create=True)
        tmp = layer_dir / f'_{name}.tmp.json'
        tmp.write_text(json.dumps(payload), encoding='utf-8')
        try:
            rel = conn.read_json(str(tmp))
            store.write_parquet(conn, rel, layer, name)
        finally:
            tmp.unlink(missing_ok=True)


@pytest.fixture()
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a lake with oracle_cards + card_otag under an isolated data dir."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    _write_layer(_ORACLE_CARDS, 'raw', 'oracle_cards')
    _write_layer(_CARD_OTAG, 'normalized', 'card_otag')
    return root


@pytest.fixture()
def empty_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated data dir with NO lake tables materialized."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    return root


class _BoomClient:
    """An httpx client stand-in whose every request raises — proves no network."""

    def get(self, *_a: object, **_k: object) -> object:
        raise AssertionError('network access attempted on an offline lake hit')

    def close(self) -> None:  # pragma: no cover - trivial
        pass


# --------------------------------------------------------------------------- #
# Port conformance + default wiring
# --------------------------------------------------------------------------- #


def test_duckdb_resolver_satisfies_port(lake: Path) -> None:
    assert isinstance(DuckDBCardResolver(), CardResolver)


def test_default_card_resolver_is_duckdb(lake: Path) -> None:
    assert isinstance(default_card_resolver(), DuckDBCardResolver)


def test_default_resolver_no_longer_writes_json_cache(lake: Path) -> None:
    """The interim scryfall_names.json cache is retired — the lake is durable now."""
    resolver = default_card_resolver()
    resolver.get_card('Sol Ring')
    assert not (lake / 'scryfall_names.json').exists()


# --------------------------------------------------------------------------- #
# Offline resolution — a lake-present card issues ZERO network.
# --------------------------------------------------------------------------- #


def test_resolves_plain_card_offline(lake: Path) -> None:
    resolver = DuckDBCardResolver(client=_BoomClient())
    card = resolver.get_card('Sol Ring')
    assert card is not None
    assert card.name == 'Sol Ring'
    assert card.oracle_id == 'sol-ring-oid'
    assert card.mana_value == 1.0
    assert card.type_line == 'Artifact'
    assert card.produced_mana == ['C']
    # presentation fields from the widened projection.
    assert card.art_crop == 'https://img/sol-ring.jpg'
    assert card.image_normal == 'https://img/sol-ring-normal.jpg'
    assert card.scryfall_uri == 'https://scryfall.com/card/c21/263/sol-ring'
    assert card.set_name == 'Commander 2021'


def test_power_toughness_stay_strings(lake: Path) -> None:
    card = DuckDBCardResolver(client=_BoomClient()).get_card('Llanowar Elves')
    assert card is not None
    assert card.power == '1'
    assert card.toughness == '1'
    assert isinstance(card.power, str)


def test_resolves_dfc_full_name_offline(lake: Path) -> None:
    resolver = DuckDBCardResolver(client=_BoomClient())
    card = resolver.get_card('Fable of the Mirror-Breaker // Reflection of Kiki-Jiki')
    assert card is not None
    assert card.oracle_id == 'fable-oid'


def _empty_list_card(name: str, oid: str) -> dict[str, Any]:
    """An oracle_cards row whose list columns are all empty (the JSON[] trigger)."""
    return {
        'oracle_id': oid,
        'name': name,
        'cmc': 1.0,
        'mana_cost': '{1}',
        'type_line': 'Artifact',
        'colors': [],
        'color_identity': [],
        'produced_mana': [],
        'keywords': [],
        'oracle_text': 'x',
        'power': None,
        'toughness': None,
        'art_crop': 'https://img/x.jpg',
        'image_normal': 'https://img/x-normal.jpg',
        'scryfall_uri': 'https://scryfall.com/x',
        'set_name': 'Test',
    }


def test_land_card_into_json_list_typed_bulk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A bulk whose list columns are empty in EVERY row makes read_json infer them as
    # JSON[] (not VARCHAR[]). Landing a live card with real colors/keywords used to
    # fail on that type mismatch ('Malformed JSON ... Input: "W"') and the card was
    # never landed (#50). It must now land, with CLEAN (unquoted) list values.
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    _write_layer([_empty_list_card('Alpha', 'a'), _empty_list_card('Beta', 'b')], 'raw', 'oracle_cards')

    resolver_mod._land_card(
        {
            'oracle_id': 'live-oid',
            'name': 'Live Keyworded',
            'cmc': 2.0,
            'mana_cost': '{1}{W}',
            'type_line': 'Creature',
            'colors': ['W'],
            'color_identity': ['W'],
            'produced_mana': [],
            'keywords': ['Flying', 'Lifelink'],
            'oracle_text': 'z',
            'prices': {},
        }
    )

    card = DuckDBCardResolver(client=_BoomClient()).get_card('Live Keyworded')
    assert card is not None  # landed durably (offline hit, no network)
    assert card.color_identity == ['W']  # clean value, NOT '"W"'
    assert card.keywords == ['Flying', 'Lifelink']
    # The pre-existing all-empty rows still read as clean empty lists.
    assert DuckDBCardResolver(client=_BoomClient()).get_card('Alpha').colors == []


def test_resolves_dfc_front_face_name_with_otags(lake: Path) -> None:
    # #49: a decklist lists the FRONT face; it must resolve to the full card AND
    # pick up its otags (keyed by the full-name oid) — offline, no live fetch.
    card = DuckDBCardResolver(client=_BoomClient()).get_card('Fable of the Mirror-Breaker')
    assert card is not None
    assert card.oracle_id == 'fable-oid'
    assert card.otags == ['token-increaser']
    assert card.otag_buckets == ['tokens']  # crosswalk: token-increaser -> tokens


def test_resolves_dfc_back_face_name(lake: Path) -> None:
    # The BACK face resolves to the SAME full card (both faces are one card).
    card = DuckDBCardResolver(client=_BoomClient()).get_card('Reflection of Kiki-Jiki')
    assert card is not None
    assert card.oracle_id == 'fable-oid'
    assert card.otag_buckets == ['tokens']


def test_exact_name_wins_over_face_match(lake: Path) -> None:
    # 'Split Alpha' is BOTH a distinct plain card AND the front face of
    # 'Split Alpha // Split Beta'. The exact match must win — no shadowing.
    card = DuckDBCardResolver(client=_BoomClient()).get_card('Split Alpha')
    assert card is not None
    assert card.oracle_id == 'alpha-oid'  # the plain card, NOT the DFC front face
    assert card.otag_buckets == ['counterspells']


def test_back_face_only_name_resolves_the_dfc(lake: Path) -> None:
    # 'Split Beta' exists ONLY as the back face -> the DFC (back-face match).
    card = DuckDBCardResolver(client=_BoomClient()).get_card('Split Beta')
    assert card is not None
    assert card.oracle_id == 'split-oid'
    assert card.otag_buckets == ['removal']


def test_resolves_apostrophe_name_offline(lake: Path) -> None:
    resolver = DuckDBCardResolver(client=_BoomClient())
    card = resolver.get_card("Jaya's Immolating Inferno")
    assert card is not None
    assert card.oracle_id == 'jaya-oid'


def test_lookup_is_case_insensitive(lake: Path) -> None:
    card = DuckDBCardResolver(client=_BoomClient()).get_card('sol ring')
    assert card is not None
    assert card.oracle_id == 'sol-ring-oid'


# --------------------------------------------------------------------------- #
# otag join — otags (raw slugs) + otag_buckets (crosswalked)
# --------------------------------------------------------------------------- #


def test_otag_join_populates_otags_and_buckets(lake: Path) -> None:
    card = DuckDBCardResolver(client=_BoomClient()).get_card('Sol Ring')
    assert card is not None
    assert set(card.otags) == {'ramp', 'mana-rock'}
    # 'ramp' is a crosswalk bucket root -> the card counts in the ramp bucket.
    assert 'ramp' in card.otag_buckets


def test_card_without_otags_has_empty_lists(lake: Path) -> None:
    """A card present in the bulk but absent from card_otag resolves with empties."""
    card = DuckDBCardResolver(client=_BoomClient()).get_card("Jaya's Immolating Inferno")
    assert card is not None
    assert card.otags == []
    assert card.otag_buckets == []


def test_otag_layer_absent_fails_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """oracle_cards present but card_otag missing -> card with empty otag lists."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    _write_layer(_ORACLE_CARDS, 'raw', 'oracle_cards')  # no card_otag
    card = DuckDBCardResolver(client=_BoomClient()).get_card('Sol Ring')
    assert card is not None
    assert card.otags == []
    assert card.otag_buckets == []


# --------------------------------------------------------------------------- #
# Live-fallback on miss — exactly one fetch, landed durably, offline after.
# --------------------------------------------------------------------------- #


def _scryfall_named_payload(name: str) -> dict[str, Any]:
    return {
        'oracle_id': 'newcard-oid',
        'name': name,
        'cmc': 2.0,
        'mana_cost': '{1}{U}',
        'type_line': 'Instant',
        'colors': ['U'],
        'color_identity': ['U'],
        'produced_mana': [],
        'keywords': [],
        'oracle_text': 'Draw two cards.',
        'power': None,
        'toughness': None,
        'image_uris': {'art_crop': 'https://img/newcard.jpg', 'normal': 'https://img/newcard-normal.jpg'},
        'scryfall_uri': 'https://scryfall.com/card/new/1/newcard',
        'set_name': 'New Set',
    }


class _CountingTransport:
    """An httpx MockTransport wrapper counting how many GETs actually happened."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        # exact hit on first try (no fuzzy needed for this name).
        return httpx.Response(200, json=_scryfall_named_payload(self.name))


def test_miss_falls_back_to_live_and_lands_durably(lake: Path) -> None:
    """A name absent from the bulk -> one live fetch, landed, offline on 2nd call."""
    counter = _CountingTransport('Brainstorm')
    client = httpx.Client(transport=httpx.MockTransport(counter.handler))
    resolver = DuckDBCardResolver(client=client)

    card = resolver.get_card('Brainstorm')
    assert card is not None
    assert card.oracle_id == 'newcard-oid'
    assert card.art_crop == 'https://img/newcard.jpg'
    assert card.image_normal == 'https://img/newcard-normal.jpg'
    assert counter.calls == 1

    # Second call for the SAME card issues ZERO further network (landed durably).
    card2 = DuckDBCardResolver(client=_BoomClient()).get_card('Brainstorm')
    assert card2 is not None
    assert card2.oracle_id == 'newcard-oid'
    assert counter.calls == 1


def test_miss_then_fuzzy_when_exact_404(lake: Path) -> None:
    """Front-face name misses exact in the lake -> live exact 404 -> fuzzy hit."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if 'exact' in request.url.params:
            calls.append('exact')
            return httpx.Response(404, json={'object': 'error'})
        calls.append('fuzzy')
        return httpx.Response(200, json=_scryfall_named_payload('Fuzzy Match'))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    card = DuckDBCardResolver(client=client).get_card('Some Front Face')
    assert card is not None
    assert card.oracle_id == 'newcard-oid'
    assert calls == ['exact', 'fuzzy']


def test_unresolvable_miss_returns_none(lake: Path) -> None:
    """A name that resolves to nothing (404 exact + fuzzy) returns None (fail-open)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={'object': 'error'})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert DuckDBCardResolver(client=client).get_card('Nonexistent Card XYZ') is None


def test_network_error_on_miss_fails_open_to_none(lake: Path) -> None:
    """Network down on a bulk miss -> None (never crashes the consumer)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('network down')

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert DuckDBCardResolver(client=client).get_card('Whatever') is None


# --------------------------------------------------------------------------- #
# Degradation — lake absent entirely -> pure live fallback.
# --------------------------------------------------------------------------- #


def test_lake_absent_degrades_to_live(empty_data_dir: Path) -> None:
    """No oracle_cards table at all -> resolve via live fetch, still landing it."""
    counter = _CountingTransport('Counterspell')
    client = httpx.Client(transport=httpx.MockTransport(counter.handler))
    card = DuckDBCardResolver(client=client).get_card('Counterspell')
    assert card is not None
    assert card.oracle_id == 'newcard-oid'
    assert counter.calls == 1


def test_lake_absent_and_network_down_returns_none(empty_data_dir: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('offline')

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert DuckDBCardResolver(client=client).get_card('Anything') is None


# --------------------------------------------------------------------------- #
# fetch_card_raw — the scryfall_cache façade helper (raw-dict shape + landing).
# --------------------------------------------------------------------------- #


def test_fetch_card_raw_returns_full_dict_and_lands(lake: Path) -> None:
    """The raw fetch returns the UNPROJECTED Scryfall dict AND lands it durably."""
    raw = {
        'oracle_id': 'raw-oid',
        'name': 'Raw Card',
        'cmc': 2.0,
        'mana_cost': '{1}{U}',
        'type_line': 'Instant',
        'color_identity': ['U'],
        'prices': {'usd': '1.23'},  # a field the Card contract drops.
        'image_uris': {'art_crop': 'https://img/raw.jpg', 'normal': 'https://img/raw-normal.jpg'},
        'scryfall_uri': 'https://scryfall.com/x',
        'set_name': 'Set',
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=raw)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    data = resolver_mod.fetch_card_raw('Raw Card', client=client)
    assert data is not None
    # Full raw shape preserved (prices survives — Card would have dropped it).
    assert data['prices'] == {'usd': '1.23'}
    assert data['oracle_id'] == 'raw-oid'

    # Landed durably -> the resolver now serves it offline as a projected Card.
    card = DuckDBCardResolver(client=_BoomClient()).get_card('Raw Card')
    assert card is not None
    assert card.oracle_id == 'raw-oid'
    assert card.art_crop == 'https://img/raw.jpg'
    assert card.image_normal == 'https://img/raw-normal.jpg'


def test_fetch_card_raw_unresolved_returns_none(lake: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={'object': 'error'})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert resolver_mod.fetch_card_raw('Nope XYZ', client=client) is None


# --------------------------------------------------------------------------- #
# Boundary hydration: the extended card-dim fields (presentation + otags)
# survive hydrate-on-read at the STORE boundary, end to end.
#
# A REAL lake (oracle_cards with presentation + card_otag data) + a REAL
# DuckDBCardResolver + a REAL LocalYamlStore. We assert the ACTUAL values (not
# just structural presence): `art_crop` (presentation) and `otags` (functional)
# make it from the Parquet through the resolver and out of the store's hydrated
# contract objects.
# --------------------------------------------------------------------------- #


def test_extended_fields_survive_hydrate_on_read_at_store_boundary(lake: Path) -> None:
    """Deck + inventory read through LocalYamlStore carry the lake's actual
    art_crop + otags values (offline, real resolver, real store)."""
    from pipeline.collection.adapters.local_yaml import LocalYamlStore

    # Seed hand-editable YAML referencing lake-present cards (Sol Ring has otags).
    root = lake / 'collection'
    (root / 'decks').mkdir(parents=True, exist_ok=True)
    (root / 'decks' / 'ramp-deck.yaml').write_text(
        'name: Ramp Deck\ncards:\n  - card: Sol Ring\n  - card: Llanowar Elves\n'
    )
    (root / 'inventory.yaml').write_text('- card: Sol Ring\n  owned: 2\n')

    store_ = LocalYamlStore(resolver=DuckDBCardResolver(client=_BoomClient()))

    # Deck boundary: the hydrated DeckCards carry presentation + functional fields.
    deck = store_.get_deck('Ramp Deck')
    sol = next(c for c in deck.cards if c.name == 'Sol Ring')
    assert sol.art_crop == 'https://img/sol-ring.jpg'  # presentation, actual value
    assert sol.image_normal == 'https://img/sol-ring-normal.jpg'
    assert set(sol.otags) == {'ramp', 'mana-rock'}  # functional, actual values
    assert 'ramp' in sol.otag_buckets
    assert sol.set_name == 'Commander 2021'

    llan = next(c for c in deck.cards if c.name == 'Llanowar Elves')
    assert llan.power == '1'  # presentation string preserved
    assert set(llan.otags) == {'ramp', 'mana-dork'}

    # Inventory boundary: the same enrichment hydrates onto the OwnedCard.
    owned = store_.list_inventory()
    assert len(owned) == 1
    assert owned[0].name == 'Sol Ring'
    assert owned[0].owned == 2  # the persisted, non-derivable fact
    assert owned[0].art_crop == 'https://img/sol-ring.jpg'  # hydrated presentation
    assert set(owned[0].otags) == {'ramp', 'mana-rock'}  # hydrated functional tags
