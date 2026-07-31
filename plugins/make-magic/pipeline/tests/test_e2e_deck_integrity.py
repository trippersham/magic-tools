"""Phase 6 — behavioral END-TO-END verification of the deck-integrity feature.

Drives the REAL CLI (``pipeline.collection.run.main`` via ``sys.argv``) end-to-end
against the LOCAL backend (``LocalYamlStore`` + the DuckDB history mirror) in a tmp
``MAKE_MAGIC_DATA_DIR`` — no network, no Airtable, no mocks of the feature under
test. Only the card-enrichment resolver is stubbed (``default_card_resolver`` ->
name-only), which is the sanctioned offline seam; every guard, the mirror, and the
audit/recover verbs run for real.

The scenario mirrors the production incident + recovery loop, proving the whole
prevent -> detect -> recover chain works as a user experiences it:

    1. Seed TWO at-target (100) Commander decks that SHARE a staple (Sol Ring).
    2. Baseline capture via ``audit-decks`` (records the known-good 100 state);
       both decks report OK.
    3. PREVENTION: ``remove-card "Sol Ring"`` WITHOUT ``--force`` ABORTS, enumerates
       BOTH affected decks, and leaves the inventory row + both decks intact.
    4. DRIFT: a naive user forces the delete (``--force``) and — simulating the
       Airtable link-cascade that the local YAML store does NOT itself perform —
       drops Sol Ring out of one deck, taking it to 99.
    5. DETECTION: ``audit-decks`` flags that deck UNDER-TARGET and lists Sol Ring
       tagged ``deleted-row`` (the inventory row is gone).
    6. RECOVERY: ``recover-decks`` (dry-run) proposes restoring Sol Ring, predicts
       100, and writes nothing; ``recover-decks --confirm`` restores it to exactly
       100.
    7. The mirror's known-good baseline was never overwritten by the drift.

A network deny (socket connect/create_connection) is installed so an accidental
live fetch fails loudly rather than silently reaching out.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from pipeline import store as _store_mod
from pipeline.collection import run as cli
from pipeline.contracts import Card

# --------------------------------------------------------------------------- #
# Offline harness — real CLI, real local backend, stubbed card enrichment only.
# --------------------------------------------------------------------------- #


class _StubResolver:
    """Name-only card resolver: keeps enrichment offline (no Scryfall live fetch)."""

    def get_card(self, name: str) -> Card | None:
        return None


def _deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any real socket connect blows up — proves the local path never phones home."""

    def _blocked(*_args: object, **_kwargs: object):
        raise AssertionError('network access attempted in offline local e2e')

    monkeypatch.setattr(socket.socket, 'connect', _blocked)
    monkeypatch.setattr(socket, 'create_connection', _blocked)


@pytest.fixture()
def local_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp local backend: MAKE_MAGIC_DATA_DIR + local backend + stub resolver."""
    root = tmp_path / 'data'
    monkeypatch.setenv(_store_mod.ENV_DATA_DIR, str(root))
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _StubResolver())
    _deny_network(monkeypatch)
    return root


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    """Dispatch the REAL CLI entry point for a verb (drives ``main`` via sys.argv)."""
    monkeypatch.setattr('sys.argv', ['collection', *argv])
    cli.main()


def _deck_json(tmp_path: Path, name: str, *, extra_cards: int, fmt: str | None = 'Commander') -> Path:
    """Write a Deck JSON: Sol Ring (commander) + ``extra_cards`` unique maindeck cards.

    ``extra_cards`` = 99 yields a 100-card Commander deck (1 commander + 99 maindeck).
    Card names are namespaced by deck so the two decks share ONLY Sol Ring.
    """
    cards: list[dict[str, object]] = [{'name': 'Sol Ring', 'role': 'commander'}]
    cards += [{'name': f'{name} Card {i}'} for i in range(extra_cards)]
    payload: dict[str, object] = {'name': name, 'cards': cards}
    if fmt is not None:
        payload['format'] = fmt
    path = tmp_path / f'{name.replace(" ", "_")}.json'
    path.write_text(json.dumps(payload))
    return path


def _deck_size(name: str) -> int:
    """Read a deck back through a fresh local store and return Σ quantities."""
    from pipeline.collection.adapters.local_yaml import LocalYamlStore

    store = LocalYamlStore(resolver=_StubResolver())
    return sum(c.quantity for c in store.get_deck(name).cards)


def _inventory_names() -> set[str]:
    from pipeline.collection.adapters.local_yaml import LocalYamlStore

    store = LocalYamlStore(resolver=_StubResolver())
    return {c.name for c in store.list_inventory()}


def _baseline_size() -> int | None:
    """The mirror's known-good baseline size for Alpha EDH (the recovery reference)."""
    from pipeline import store as _lake
    from pipeline.collection import last_known_good_deck

    with _lake.connect() as conn:
        good = last_known_good_deck(conn, 'Alpha EDH', 'local')
    return None if good is None else int(good['size'])


# --------------------------------------------------------------------------- #
# The scenario — one executable narrative, asserted step by step.
# --------------------------------------------------------------------------- #


def test_e2e_prevent_detect_recover(
    local_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    # --- 1. SEED: two at-target Commander decks sharing Sol Ring ------------- #
    _run(monkeypatch, 'add-card', 'Sol Ring')
    alpha = _deck_json(tmp_path, 'Alpha EDH', extra_cards=99)
    beta = _deck_json(tmp_path, 'Beta EDH', extra_cards=99)
    _run(monkeypatch, 'save-deck', '--from-json', str(alpha))
    _run(monkeypatch, 'save-deck', '--from-json', str(beta))
    assert _deck_size('Alpha EDH') == 100
    assert _deck_size('Beta EDH') == 100
    capsys.readouterr()

    # --- 2. BASELINE CAPTURE: audit records the known-good 100 state --------- #
    _run(monkeypatch, 'audit-decks', '--json')
    audit = json.loads(capsys.readouterr().out)
    by_name = {d['name']: d for d in audit['decks']}
    assert by_name['Alpha EDH']['status'] == 'OK'
    assert by_name['Beta EDH']['status'] == 'OK'
    assert audit['under_target'] == 0
    # The mirror now holds the known-good 100 baseline.
    assert _baseline_size() == 100

    # --- 3. PREVENTION: remove-card WITHOUT --force ABORTS ------------------- #
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 'remove-card', 'Sol Ring')
    assert exc.value.code != 0
    err = capsys.readouterr().err
    # Enumerates BOTH decks that would be cascade-stripped, flags under-target.
    assert 'Alpha EDH' in err
    assert 'Beta EDH' in err
    assert 'UNDER TARGET' in err
    assert '100 -> 99' in err
    # The guard held: the row still exists and both decks are still 100.
    assert 'Sol Ring' in _inventory_names()
    assert _deck_size('Alpha EDH') == 100
    assert _deck_size('Beta EDH') == 100

    # --- 4. DRIFT: a naive user forces it, then the cascade drops the card --- #
    # `--force` deletes the shared inventory row. The LOCAL store does not itself
    # cascade the deck YAML, so we simulate the Airtable link-cascade that WOULD
    # have stripped Sol Ring from the deck — dropping Alpha EDH to 99.
    _run(monkeypatch, 'remove-card', 'Sol Ring', '--force')
    assert 'Sol Ring' not in _inventory_names()
    capsys.readouterr()
    drifted = tmp_path / 'alpha_drifted.json'
    drifted.write_text(json.dumps({'name': 'Alpha EDH', 'format': 'Commander',
                                   'cards': [{'name': f'Alpha EDH Card {i}'} for i in range(99)]}))
    _run(monkeypatch, 'save-deck', '--from-json', str(drifted), '--confirm')
    assert _deck_size('Alpha EDH') == 99
    capsys.readouterr()

    # --- 5. DETECTION: audit flags UNDER-TARGET and diffs Sol Ring ---------- #
    _run(monkeypatch, 'audit-decks', '--json')
    audit2 = json.loads(capsys.readouterr().out)
    by_name2 = {d['name']: d for d in audit2['decks']}
    assert by_name2['Alpha EDH']['status'] == 'UNDER-TARGET'
    assert by_name2['Beta EDH']['status'] == 'OK'
    assert audit2['under_target'] == 1
    assert by_name2['Alpha EDH']['baseline'] is True
    missing = {m['name']: m['tag'] for m in by_name2['Alpha EDH']['missing']}
    assert 'Sol Ring' in missing
    # Row was hard-deleted, so it is tagged deleted-row (not merely unlinked).
    assert missing['Sol Ring'].startswith('deleted-row')

    # --- 6a. RECOVERY (dry-run): proposes 100, writes NOTHING --------------- #
    _run(monkeypatch, 'recover-decks', 'Alpha EDH')
    dry = capsys.readouterr().out
    assert 'Sol Ring' in dry
    assert 'predicted size 100' in dry
    assert 'dry-run' in dry.lower()
    # Nothing was written: the deck is still 99, the row still gone.
    assert _deck_size('Alpha EDH') == 99
    assert 'Sol Ring' not in _inventory_names()

    # --- 6b. RECOVERY (--confirm): restores exactly 100 --------------------- #
    _run(monkeypatch, 'recover-decks', 'Alpha EDH', '--confirm')
    applied = capsys.readouterr().out
    assert 'OK' in applied
    assert _deck_size('Alpha EDH') == 100
    # The deleted inventory row was recreated so the re-link could resolve.
    assert 'Sol Ring' in _inventory_names()

    # --- 7. The known-good baseline was NEVER overwritten by the drift ------ #
    # audit-decks captured drift rows into the append-only mirror, but the newest
    # AT-TARGET (passed) row for Alpha EDH is still the original 100 baseline.
    assert _baseline_size() == 100

    # Post-recovery audit: both decks OK again (full loop closed).
    _run(monkeypatch, 'audit-decks', '--json')
    audit3 = json.loads(capsys.readouterr().out)
    assert all(d['status'] in ('OK', 'untargeted') for d in audit3['decks'])
    assert audit3['under_target'] == 0
