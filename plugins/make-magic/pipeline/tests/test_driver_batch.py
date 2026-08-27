"""Phase 3.1/3.2 — corpus classification harness (the CHEAP classify pass, no sims).

Unit tests for the locked classify rules (rule 1 drive/thin, rule 2 fewest-piece /
commander tiebreak, rule 4 Φ-mode) and for the restartable per-deck ledger. NO JVM,
NO combo lake, NO sims: every test drives ``classify_deck`` with synthetic combos and
in-memory :class:`CorpusDeck` subjects, and exercises the ledger against ``tmp_path``.
"""

from __future__ import annotations

import json

from pipeline.sim import driver_batch as db
from pipeline.transforms.combo_detect import Combo

# --------------------------------------------------------------------------- #
# Synthetic combos + corpus decks.
# --------------------------------------------------------------------------- #


def _combo(vid: str, names: tuple[str, ...], result: str) -> Combo:
    return Combo(variant_id=vid, card_names=names, card_oracle_ids=('',) * len(names), result=result)


# A game-WIN two-piece combo (rule-1 qualifies) and a bare-resource loop (does not).
_WIN_2 = _combo('win2', ('Thassa\'s Oracle', 'Demonic Consultation'), 'Win the game')
_WIN_3 = _combo('win3', ('Kiki-Jiki, Mirror Breaker', 'Zealous Conscripts', 'Sol Ring'), 'Infinite damage')
_LOOP = _combo('loop', ('Basalt Monolith', 'Rings of Brighthearth'), 'Infinite colorless mana')


def _deck(**kw: object) -> db.CorpusDeck:
    base: dict[str, object] = {
        'deck_id': 'g/x.dck',
        'name': 'X',
        'source': 'gauntlet',
        'fmt': 'commander',
        'card_names': (),
        'commander_names': (),
        'strategy': None,
    }
    base.update(kw)
    return db.CorpusDeck(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Rule 1 — drive/thin.
# --------------------------------------------------------------------------- #


def test_deck_with_win_combo_is_drive() -> None:
    deck = _deck(card_names=("Thassa's Oracle", 'Demonic Consultation', 'Island'))
    res = db.classify_deck(deck, [_WIN_2, _LOOP])
    assert res['drive'] is True
    assert res['detected_win_combos']
    assert res['chosen_combo']['variant_id'] == 'win2'


def test_deck_with_only_resource_loop_is_thin() -> None:
    # Has a full combo, but its result is a bare resource loop -> rule 1 THIN.
    deck = _deck(card_names=('Basalt Monolith', 'Rings of Brighthearth', 'Forest'))
    res = db.classify_deck(deck, [_WIN_2, _LOOP])
    assert res['drive'] is False
    assert res['archetype'] == 'thin'
    assert res['chosen_combo'] is None


def test_deck_missing_a_piece_is_thin() -> None:
    deck = _deck(card_names=("Thassa's Oracle", 'Island'))  # no Consultation
    res = db.classify_deck(deck, [_WIN_2])
    assert res['drive'] is False


# --------------------------------------------------------------------------- #
# Rule 2 — fewest-pieces / commander tiebreak + multi-combo flag.
# --------------------------------------------------------------------------- #


def test_tiebreak_prefers_fewest_pieces() -> None:
    deck = _deck(
        card_names=(
            "Thassa's Oracle",
            'Demonic Consultation',
            'Kiki-Jiki, Mirror Breaker',
            'Zealous Conscripts',
            'Sol Ring',
        )
    )
    res = db.classify_deck(deck, [_WIN_3, _WIN_2])
    assert res['chosen_combo']['variant_id'] == 'win2'  # 2 pieces < 3


def test_tiebreak_prefers_commander_participation() -> None:
    # Two 2-piece win combos; commander is a piece of the SECOND -> pick it.
    win_a = _combo('a', ('Card A1', 'Card A2'), 'Win the game')
    win_b = _combo('b', ('General B', 'Card B2'), 'Win the game')
    deck = _deck(
        card_names=('Card A1', 'Card A2', 'General B', 'Card B2'),
        commander_names=('General B',),
    )
    res = db.classify_deck(deck, [win_a, win_b])
    assert res['chosen_combo']['variant_id'] == 'b'


def test_multi_combo_flag_on_equally_central_ties() -> None:
    # Two distinct 2-piece win combos, NEITHER involving the commander -> tie -> flag.
    win_a = _combo('a', ('Card A1', 'Card A2'), 'Win the game')
    win_b = _combo('b', ('Card B1', 'Card B2'), 'Win the game')
    deck = _deck(card_names=('Card A1', 'Card A2', 'Card B1', 'Card B2'))
    res = db.classify_deck(deck, [win_a, win_b])
    assert 'multi-combo' in res['flags']
    # Deterministic first pick.
    assert res['chosen_combo']['variant_id'] == 'a'


# --------------------------------------------------------------------------- #
# Rule 4 — Φ mode.
# --------------------------------------------------------------------------- #


def test_commander_is_piece_is_drive_dedicated() -> None:
    win = _combo('c', ('General C', 'Card C2'), 'Win the game')
    deck = _deck(card_names=('General C', 'Card C2'), commander_names=('General C',))
    res = db.classify_deck(deck, [win])
    assert res['archetype'] == 'drive-dedicated'
    assert 'phi-mode-defaulted' not in res['flags']


def test_strategy_names_combo_is_drive_dedicated() -> None:
    deck = _deck(
        card_names=("Thassa's Oracle", 'Demonic Consultation'),
        strategy="The plan is to win with Thassa's Oracle after Demonic Consultation.",
    )
    res = db.classify_deck(deck, [_WIN_2])
    assert res['archetype'] == 'drive-dedicated'


def test_three_tutors_is_drive_dedicated() -> None:
    deck = _deck(
        card_names=(
            "Thassa's Oracle",
            'Demonic Consultation',
            'Demonic Tutor',
            'Vampiric Tutor',
            'Diabolic Intent',
        )
    )
    res = db.classify_deck(deck, [_WIN_2])
    assert res['archetype'] == 'drive-dedicated'


def test_borderline_drive_defaults_capable_with_flag() -> None:
    # Drive (win combo present) but commander not a piece, no strategy, no tutors.
    deck = _deck(card_names=("Thassa's Oracle", 'Demonic Consultation'))
    res = db.classify_deck(deck, [_WIN_2])
    assert res['archetype'] == 'drive-capable'
    assert 'phi-mode-defaulted' in res['flags']


def test_thin_deck_wincon_style_and_archetype() -> None:
    deck = _deck(card_names=('Forest', 'Grizzly Bears'))
    res = db.classify_deck(deck, [_WIN_2])
    assert res['archetype'] == 'thin'
    assert res['wincon_style'] == db.THIN_WINCON_STYLE


def test_drive_wincon_style_is_combo() -> None:
    deck = _deck(card_names=("Thassa's Oracle", 'Demonic Consultation'))
    res = db.classify_deck(deck, [_WIN_2])
    assert res['wincon_style'] == db.DRIVE_WINCON_STYLE


# --------------------------------------------------------------------------- #
# The restartable ledger.
# --------------------------------------------------------------------------- #


def test_ledger_roundtrip_and_atomic_write(tmp_path) -> None:
    path = tmp_path / 'ledger.jsonl'
    ledger = db.Ledger(path)
    ledger.record({'deck_id': 'a', 'stage': 'classified', 'drive': True})
    ledger.record({'deck_id': 'b', 'stage': 'classified', 'drive': False})

    reloaded = db.Ledger(path)
    assert reloaded.stage_of('a') == 'classified'
    assert reloaded.stage_of('b') == 'classified'
    assert reloaded.stage_of('missing') is None
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert {r['deck_id'] for r in rows} == {'a', 'b'}


def test_ledger_last_write_wins_per_deck(tmp_path) -> None:
    path = tmp_path / 'ledger.jsonl'
    ledger = db.Ledger(path)
    ledger.record({'deck_id': 'a', 'stage': 'classified', 'drive': False})
    ledger.record({'deck_id': 'a', 'stage': 'authored', 'drive': True})
    reloaded = db.Ledger(path)
    assert reloaded.stage_of('a') == 'authored'
    assert reloaded.row('a')['drive'] is True


def test_classify_stage_is_restartable(tmp_path) -> None:
    path = tmp_path / 'ledger.jsonl'
    decks = [
        _deck(deck_id='d1', card_names=("Thassa's Oracle", 'Demonic Consultation')),
        _deck(deck_id='d2', card_names=('Forest',)),
    ]

    # First pass classifies both.
    first = db.run_classify(decks, [_WIN_2], ledger_path=path)
    assert first['classified'] == 2
    assert first['skipped'] == 0

    # Second pass skips both (already at/after the target stage).
    second = db.run_classify(decks, [_WIN_2], ledger_path=path)
    assert second['classified'] == 0
    assert second['skipped'] == 2


def test_stage_rank_ordering() -> None:
    assert db.stage_rank('classified') < db.stage_rank('authored')
    assert db.stage_rank('authored') < db.stage_rank('compiled')
    assert db.stage_rank('compiled') < db.stage_rank('gated')
