"""Tests for the DECK-EDIT CLI verbs (Phase 3a — the guided-build surface).

OFFLINE: a tmp MAKE_MAGIC_DATA_DIR + local YAML backend + a stub resolver (no
scripts/ import, no Scryfall, no network). These verbs are thin wrappers over the
Phase-1/2 ``DecksStore`` / ``sync`` methods — the tests assert the CLI plumbing
(name -> deck_id resolution, guard surfacing, ephemeral lifecycle), not the store
semantics (already covered by the store/sync tests).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import store
from pipeline.collection import run as cli
from pipeline.contracts import Card


class _StubResolver:
    def get_card(self, name: str) -> Card | None:
        return None


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _StubResolver())
    return root


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr('sys.argv', ['collection', *argv])
    cli.main()


def _save_source_deck(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    cards: list[dict[str, object]],
    *,
    format_: str | None = None,
) -> None:
    """Save a deck through the CLI so it lands in the LocalYamlStore source."""
    payload: dict[str, object] = {'name': name, 'cards': cards}
    if format_ is not None:
        payload['format'] = format_
    deck_json = tmp_path / f'{name}.json'
    deck_json.write_text(json.dumps(payload))
    _run(monkeypatch, 'save-deck', '--from-json', str(deck_json))


# A 100-card Commander deck (99 filler + 1 commander) so shrink/target guards are
# exercisable: a Commander deck's target_size is 100.
def _commander_cards(commander: str = 'Grumgully, the Generous') -> list[dict[str, object]]:
    cards: list[dict[str, object]] = [{'name': commander, 'role': 'commander'}]
    cards.extend({'name': f'Filler {i}'} for i in range(99))
    return cards


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #


def test_deck_edit_verbs_registered() -> None:
    for verb in ('deck-swap', 'deck-add', 'deck-remove', 'new-draft', 'promote-deck', 'undo-deck'):
        assert verb in cli.VERBS


# --------------------------------------------------------------------------- #
# deck-swap
# --------------------------------------------------------------------------- #


def test_deck_swap_applies_size_preserving(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    _save_source_deck(monkeypatch, tmp_path, 'Gruul', _commander_cards(), format_='Commander')
    capsys.readouterr()

    _run(monkeypatch, 'deck-swap', 'Gruul', '--add', 'Lightning Bolt', '--cut', 'Filler 0')
    capsys.readouterr()

    _run(monkeypatch, 'get-deck', 'Gruul')
    deck = json.loads(capsys.readouterr().out)
    names = {c['name']: c for c in deck['cards']}
    assert 'Lightning Bolt' in names
    assert 'Filler 0' not in names
    # size preserved (100 in, 100 out).
    assert sum(c['quantity'] for c in deck['cards']) == 100


def test_deck_swap_commander_violation_exits_nonzero_and_leaves_deck(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    _save_source_deck(monkeypatch, tmp_path, 'Gruul', _commander_cards(), format_='Commander')
    capsys.readouterr()

    # Cutting the sole commander is refused by the store guard.
    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, 'deck-swap', 'Gruul', '--add', 'Lightning Bolt', '--cut', 'Grumgully, the Generous')
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith('error: ')
    assert 'Traceback' not in err

    # The deck is untouched — commander still present, the add never happened.
    _run(monkeypatch, 'get-deck', 'Gruul')
    deck = json.loads(capsys.readouterr().out)
    names = {c['name'] for c in deck['cards']}
    assert 'Grumgully, the Generous' in names
    assert 'Lightning Bolt' not in names


# --------------------------------------------------------------------------- #
# deck-add / deck-remove (quantity-aware)
# --------------------------------------------------------------------------- #


def test_deck_add_and_remove_quantity_aware(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    _save_source_deck(monkeypatch, tmp_path, 'Gruul', _commander_cards(), format_='Commander')
    capsys.readouterr()

    _run(monkeypatch, 'deck-add', 'Gruul', 'Forest', '--qty', '3')
    capsys.readouterr()
    _run(monkeypatch, 'get-deck', 'Gruul')
    deck = json.loads(capsys.readouterr().out)
    forest = next(c for c in deck['cards'] if c['name'] == 'Forest')
    assert forest['quantity'] == 3

    # Remove 1 -> quantity-aware decrement, entry survives at 2.
    _run(monkeypatch, 'deck-remove', 'Gruul', 'Forest', '--qty', '1')
    capsys.readouterr()
    _run(monkeypatch, 'get-deck', 'Gruul')
    deck = json.loads(capsys.readouterr().out)
    forest = next(c for c in deck['cards'] if c['name'] == 'Forest')
    assert forest['quantity'] == 2


def test_deck_remove_under_target_exits_nonzero(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    # Exactly-at-target (100) Commander deck: removing a card drops it below 100.
    _save_source_deck(monkeypatch, tmp_path, 'Gruul', _commander_cards(), format_='Commander')
    capsys.readouterr()

    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, 'deck-remove', 'Gruul', 'Filler 0')
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith('error: ')
    assert 'Traceback' not in err


# --------------------------------------------------------------------------- #
# new-draft (ephemeral) — clean-slate + copy-from
# --------------------------------------------------------------------------- #


def test_new_draft_clean_slate_creates_ephemeral(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run(monkeypatch, 'new-draft', 'Fresh Idea', '--commander', 'Atraxa', '--format', 'Commander')
    capsys.readouterr()

    _run(monkeypatch, 'list-decks', '--json')
    rows = {r['name']: r['status'] for r in json.loads(capsys.readouterr().out)}
    assert rows['Fresh Idea'] == 'ephemeral'

    # The commander landed as a DeckCard with role=commander.
    _run(monkeypatch, 'get-deck', 'Fresh Idea')
    deck = json.loads(capsys.readouterr().out)
    assert deck['format'] == 'Commander'
    assert [c['name'] for c in deck['cards'] if c['role'] == 'commander'] == ['Atraxa']


def test_new_draft_from_copies_source_locally_leaving_original(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    _save_source_deck(monkeypatch, tmp_path, 'Original', _commander_cards(), format_='Commander')
    capsys.readouterr()

    _run(monkeypatch, 'new-draft', 'Experiment', '--from', 'Original')
    capsys.readouterr()

    # The draft shows up as ephemeral; the source deck still shows as synced.
    _run(monkeypatch, 'list-decks', '--json')
    rows = {r['name']: r['status'] for r in json.loads(capsys.readouterr().out)}
    assert rows['Experiment'] == 'ephemeral'
    assert rows['Original'] == 'synced'

    # The copy carries the source's cards (a genuine exploration copy).
    _run(monkeypatch, 'get-deck', 'Experiment')
    copy_deck = json.loads(capsys.readouterr().out)
    assert sum(c['quantity'] for c in copy_deck['cards']) == 100

    # Editing the DRAFT does not touch the original source deck.
    _run(monkeypatch, 'deck-swap', 'Experiment', '--add', 'Lightning Bolt', '--cut', 'Filler 0')
    capsys.readouterr()
    _run(monkeypatch, 'get-deck', 'Original')
    original = json.loads(capsys.readouterr().out)
    names = {c['name'] for c in original['cards']}
    assert 'Lightning Bolt' not in names
    assert 'Filler 0' in names


# --------------------------------------------------------------------------- #
# promote-deck (ephemeral -> synced through the ceremony)
# --------------------------------------------------------------------------- #


def test_promote_deck_turns_ephemeral_into_synced_on_source(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    _run(monkeypatch, 'new-draft', 'Brew', '--commander', 'Atraxa', '--format', 'Commander')
    capsys.readouterr()

    # Before promotion: ephemeral, absent from the source.
    _run(monkeypatch, 'list-decks', '--json')
    before = {r['name']: r['status'] for r in json.loads(capsys.readouterr().out)}
    assert before['Brew'] == 'ephemeral'

    _run(monkeypatch, 'promote-deck', 'Brew', '--to', 'Brew')
    capsys.readouterr()

    # After promotion: the deck now exists on the source (synced).
    _run(monkeypatch, 'list-decks', '--json')
    after = {r['name']: r['status'] for r in json.loads(capsys.readouterr().out)}
    assert after.get('Brew') == 'synced'

    # It is genuinely readable back through the source-backed get-deck.
    _run(monkeypatch, 'get-deck', 'Brew')
    deck = json.loads(capsys.readouterr().out)
    assert [c['name'] for c in deck['cards'] if c['role'] == 'commander'] == ['Atraxa']


# --------------------------------------------------------------------------- #
# undo-deck
# --------------------------------------------------------------------------- #


def test_undo_deck_restores_prior_version(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    _save_source_deck(monkeypatch, tmp_path, 'Gruul', _commander_cards(), format_='Commander')
    capsys.readouterr()

    _run(monkeypatch, 'deck-swap', 'Gruul', '--add', 'Lightning Bolt', '--cut', 'Filler 0')
    capsys.readouterr()
    # Confirm the edit applied.
    _run(monkeypatch, 'get-deck', 'Gruul')
    edited = {c['name'] for c in json.loads(capsys.readouterr().out)['cards']}
    assert 'Lightning Bolt' in edited and 'Filler 0' not in edited

    _run(monkeypatch, 'undo-deck', 'Gruul')
    capsys.readouterr()

    _run(monkeypatch, 'get-deck', 'Gruul')
    restored = {c['name'] for c in json.loads(capsys.readouterr().out)['cards']}
    assert 'Lightning Bolt' not in restored
    assert 'Filler 0' in restored


def test_undo_deck_nothing_to_undo(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run(monkeypatch, 'new-draft', 'Empty Brew', '--commander', 'Atraxa', '--format', 'Commander')
    capsys.readouterr()
    # A freshly-created draft has a single (floor) version — nothing to undo to.
    _run(monkeypatch, 'undo-deck', 'Empty Brew')
    out = capsys.readouterr().out.lower()
    assert 'nothing to undo' in out


# --------------------------------------------------------------------------- #
# deck-combos — the VALIDATE archetype-fidelity signal + honest degradation
# --------------------------------------------------------------------------- #


def _combo(variant_id: str, *cards: str, result: str = 'Infinite mana') -> object:
    """A minimal ``Combo`` (concrete named cards only) for the fidelity tests."""
    from pipeline.transforms.combo_detect import Combo

    return Combo(
        variant_id=variant_id,
        card_names=tuple(cards),
        card_oracle_ids=tuple('' for _ in cards),
        result=result,
    )


def test_deck_combos_registered() -> None:
    assert 'deck-combos' in cli.VERBS


def test_deck_combos_reports_present_combo(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    # A deck holding BOTH cards of a full combo.
    cards = _commander_cards()
    cards[1] = {'name': 'Basalt Monolith'}
    cards[2] = {'name': 'Rings of Brighthearth'}
    _save_source_deck(monkeypatch, tmp_path, 'Combo Deck', cards, format_='Commander')
    capsys.readouterr()

    combo = _combo('v1', 'Basalt Monolith', 'Rings of Brighthearth')
    monkeypatch.setattr('pipeline.transforms.combo_detect.load_combos', lambda: [combo])

    _run(monkeypatch, 'deck-combos', 'Combo Deck')
    out = json.loads(capsys.readouterr().out)
    assert out['combo_data_available'] is True
    assert [c['variant_id'] for c in out['combos']] == ['v1']
    assert set(out['combos'][0]['cards']) == {'Basalt Monolith', 'Rings of Brighthearth'}


def test_deck_combos_absent_combo_available_true(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    # The lake has a combo, but the deck holds only ONE of its two pieces -> no
    # match, and the check IS conclusive (available=true means "checked, none").
    cards = _commander_cards()
    cards[1] = {'name': 'Basalt Monolith'}
    _save_source_deck(monkeypatch, tmp_path, 'Partial Deck', cards, format_='Commander')
    capsys.readouterr()

    combo = _combo('v1', 'Basalt Monolith', 'Rings of Brighthearth')
    monkeypatch.setattr('pipeline.transforms.combo_detect.load_combos', lambda: [combo])

    _run(monkeypatch, 'deck-combos', 'Partial Deck')
    out = json.loads(capsys.readouterr().out)
    assert out['combo_data_available'] is True
    assert out['combos'] == []


def test_deck_combos_inconclusive_on_lake_failure(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    # A sparse/absent lake: load_combos RAISES -> honest degradation. available is
    # false and the match set is empty, but this is INCONCLUSIVE, not a clean bill.
    _save_source_deck(monkeypatch, tmp_path, 'Sparse Deck', _commander_cards(), format_='Commander')
    capsys.readouterr()

    def _boom() -> list:
        raise RuntimeError('combo lake not built')

    monkeypatch.setattr('pipeline.transforms.combo_detect.load_combos', _boom)

    _run(monkeypatch, 'deck-combos', 'Sparse Deck')
    out = json.loads(capsys.readouterr().out)
    assert out['combo_data_available'] is False


# --------------------------------------------------------------------------- #
# --id precedence (P2) — the escape hatch overrides the positional NAME
# --------------------------------------------------------------------------- #


def test_id_flag_disambiguates_two_active_gruul(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """Two active local rows named 'Gruul' -> `get-deck 'Gruul'` refuses; `--id` resolves one."""
    from pipeline.decks import DecksStore

    _save_source_deck(monkeypatch, tmp_path, 'Gruul', _commander_cards('Grumgully, the Generous'), format_='Commander')
    # A second, distinct ephemeral draft that reuses the name 'Gruul'.
    _run(monkeypatch, 'new-draft', 'Gruul')
    capsys.readouterr()

    # Bare name is ambiguous (2 rows) -> clean one-line refusal, exit 1.
    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, 'get-deck', 'Gruul')
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "is ambiguous (2 decks)" in err
    assert 'Traceback' not in err

    # Pick the ephemeral draft by its short id -> resolves that specific row.
    rows = DecksStore().list_rows(include_archived=True)
    eph = next(r for r in rows if r.sync_status == 'ephemeral')
    _run(monkeypatch, 'get-deck', 'Gruul', '--id', eph.deck_uuid[:6])
    out = json.loads(capsys.readouterr().out)
    assert out['uuid'] == eph.deck_uuid


def test_id_flag_precedence_over_positional_name(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """When BOTH a name and --id are given, --id wins (the name is ignored)."""
    from pipeline.decks import DecksStore

    _run(monkeypatch, 'new-draft', 'Alpha')
    _run(monkeypatch, 'new-draft', 'Beta')
    capsys.readouterr()

    rows = {r.name: r for r in DecksStore().list_rows(include_archived=True)}
    beta_uuid = rows['Beta'].deck_uuid

    # Address 'Alpha' by name but override with Beta's --id -> Beta wins.
    _run(monkeypatch, 'get-deck', 'Alpha', '--id', beta_uuid[:6])
    out = json.loads(capsys.readouterr().out)
    assert out['name'] == 'Beta'
    assert out['uuid'] == beta_uuid
