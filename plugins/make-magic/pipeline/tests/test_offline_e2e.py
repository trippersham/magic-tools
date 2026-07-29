"""Behavioral verification: the offline local-mode factsheet path (Phase 1.4).

Seeds a sample `collection/decks/gruul-aggro.yaml`, resolves it through the local
YAML adapter (with a stub resolver — NO network), runs the offline factsheet
entry point, and asserts:
    - a valid `FactSheet` with `otag_buckets` and/or a `susceptibility` signal is
      emitted;
    - the local path constructs NO Airtable client and makes no raw-socket network
      call — `socket.connect`/`create_connection` are denied and the Airtable
      `GetOnlyClient` factory is poisoned to raise if touched.

This encodes issue #6 Scenario 1's ZERO-Airtable/MCP guarantee (the criterion is
zero *Airtable/MCP*, not zero Scryfall — the otag layer fails open to its bundled
snapshot when the network is denied). Broader ingest->marts offline coverage is
tracked in #13.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from pipeline import store
from pipeline.collection.adapters.local_yaml import LocalYamlStore
from pipeline.contracts import Card, FactSheet

_SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'


def _load_deck_factsheet():
    """Import scripts/deck_factsheet.py as a module (it is a PEP-723 script)."""
    spec = importlib.util.spec_from_file_location('deck_factsheet_e2e', _SCRIPTS / 'deck_factsheet.py')
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules['deck_factsheet_e2e'] = mod
    spec.loader.exec_module(mod)
    return mod


class _CatalogResolver:
    """A stub CardResolver — a tiny in-memory Scryfall stand-in (no network)."""

    _CATALOG: ClassVar[dict[str, Card]] = {
        'Grumgully, the Generous': Card(
            name='Grumgully, the Generous',
            oracle_id='grumgully-oid',
            mana_value=3.0,
            type_line='Legendary Creature — Goblin Shaman',
            color_identity=['R', 'G'],
            oracle_text='Each other non-Human creature you control enters with an additional +1/+1 counter on it.',
        ),
        'Sol Ring': Card(
            name='Sol Ring',
            oracle_id='sol-ring-oid',
            mana_value=1.0,
            type_line='Artifact',
            produced_mana=['C'],
            oracle_text='{T}: Add {C}{C}.',
        ),
        'Llanowar Elves': Card(
            name='Llanowar Elves',
            oracle_id='llanowar-oid',
            mana_value=1.0,
            type_line='Creature — Elf Druid',
            colors=['G'],
            color_identity=['G'],
            produced_mana=['G'],
            oracle_text='{T}: Add {G}.',
        ),
        'Cultivate': Card(
            name='Cultivate',
            oracle_id='cultivate-oid',
            mana_value=3.0,
            type_line='Sorcery',
            colors=['G'],
            color_identity=['G'],
            oracle_text='Search your library for up to two basic land cards…',
        ),
        'Forest': Card(
            name='Forest',
            oracle_id='forest-oid',
            mana_value=0.0,
            type_line='Basic Land — Forest',
            color_identity=['G'],
            produced_mana=['G'],
        ),
    }

    def get_card(self, name: str) -> Card | None:
        return self._CATALOG.get(name)


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    return root


def _seed_gruul(root: Path) -> None:
    decks = root / 'collection' / 'decks'
    decks.mkdir(parents=True, exist_ok=True)
    (decks / 'gruul-aggro.yaml').write_text(
        'name: Gruul Aggro\n'
        'strategy: Go-wide creature aggro leaning on +1/+1 counter value.\n'
        'airtable_record_id: null\n'
        'cards:\n'
        # BLOCK style is the safe canonical shape: comma-containing names are
        # double-quoted and each fact sits on its own line (no flow-comma trap).
        '  - card: "Grumgully, the Generous"\n'
        '    role: commander\n'
        '  - card: Sol Ring\n'
        '  - card: Llanowar Elves\n'
        '  - card: Cultivate\n'
        '  - card: Forest\n'
        '    qty: 34\n'
    )


def _deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any real socket connect (Airtable / Scryfall / HTTP) blow up."""

    def _blocked(*_args: object, **_kwargs: object):
        raise AssertionError('network access attempted in offline local mode')

    monkeypatch.setattr(socket.socket, 'connect', _blocked)
    monkeypatch.setattr(socket, 'create_connection', _blocked)


def test_offline_factsheet_from_local_deck(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_gruul(data_dir)
    _deny_network(monkeypatch)  # asserts zero network the whole way through

    # Resolve the deck through the local adapter (offline, stubbed resolver).
    adapter = LocalYamlStore(resolver=_CatalogResolver())
    deck = adapter.get_deck('Gruul Aggro')
    assert [c.name for c in deck.commanders] == ['Grumgully, the Generous']

    # Run the offline factsheet entry point on the resolved deck.
    dfs = _load_deck_factsheet()
    report = dfs.factsheet_from_deck(deck)

    # It validates against the FactSheet contract.
    fs = FactSheet.model_validate(report)
    assert fs.deck == 'Gruul Aggro'
    # The factsheet works on distinct card RECORDS (not expanded copies): 1 land
    # record (Forest), 4 nonland records (Grumgully/Sol Ring/Llanowar/Cultivate).
    assert fs.shape.land_count == 1
    assert fs.shape.nonland_count == 4

    # otag layer populated (bundled snapshot at minimum) OR degraded with a clear
    # signal — either way otag_buckets + susceptibility are present + coherent.
    assert isinstance(fs.otag_buckets, dict)
    assert isinstance(fs.susceptibility, list)
    # The offline path produces SOME otag/susceptibility content (buckets from the
    # bundled snapshot, or the explicit "otag layer unavailable" degrade signal).
    assert fs.otag_buckets or fs.susceptibility


def test_offline_path_makes_no_airtable_import(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The offline factsheet path never imports/constructs the Airtable client."""
    _seed_gruul(data_dir)
    _deny_network(monkeypatch)

    # Poison the Airtable source module's client factory: touching it fails loudly.
    import pipeline.sources.airtable as at

    def _boom(*_a: object, **_k: object):
        raise AssertionError('Airtable client constructed in offline local mode')

    monkeypatch.setattr(at, 'GetOnlyClient', _boom)

    adapter = LocalYamlStore(resolver=_CatalogResolver())
    deck = adapter.get_deck('Gruul Aggro')
    dfs = _load_deck_factsheet()
    report = dfs.factsheet_from_deck(deck)
    assert report['deck'] == 'Gruul Aggro'


class _ManaCostResolver:
    """A stub resolver that provides `mana_cost` — exercises the deck-path pips."""

    _CATALOG: ClassVar[dict[str, Card]] = {
        'Llanowar Elves': Card(
            name='Llanowar Elves',
            oracle_id='llanowar-oid',
            mana_value=1.0,
            mana_cost='{G}',
            type_line='Creature — Elf Druid',
            colors=['G'],
            color_identity=['G'],
        ),
        'Cultivate': Card(
            name='Cultivate',
            oracle_id='cultivate-oid',
            mana_value=3.0,
            mana_cost='{2}{G}',
            type_line='Sorcery',
            colors=['G'],
            color_identity=['G'],
        ),
        'Forest': Card(
            name='Forest',
            oracle_id='forest-oid',
            mana_value=0.0,
            mana_cost='',
            type_line='Basic Land — Forest',
            color_identity=['G'],
            produced_mana=['G'],
        ),
    }

    def get_card(self, name: str) -> Card | None:
        return self._CATALOG.get(name)


def test_deck_path_pip_counts_nonzero_with_mana_cost(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a resolver that provides `mana_cost`, the deck-path factsheet computes
    non-zero `pip_counts` for a colored deck (previously always zero)."""
    decks = data_dir / 'collection' / 'decks'
    decks.mkdir(parents=True, exist_ok=True)
    (decks / 'gruul.yaml').write_text(
        'name: Gruul\ncards:\n  - card: Llanowar Elves\n  - card: Cultivate\n  - card: Forest\n    qty: 34\n'
    )
    _deny_network(monkeypatch)

    adapter = LocalYamlStore(resolver=_ManaCostResolver())
    deck = adapter.get_deck('Gruul')
    dfs = _load_deck_factsheet()
    fs = FactSheet.model_validate(dfs.factsheet_from_deck(deck))

    # {G} + {2}{G} -> two G pips over nonland cards; Forest (land) contributes none.
    assert fs.mana.pip_counts['G'] == 2
    assert sum(fs.mana.pip_counts.values()) > 0
