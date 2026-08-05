"""TDD tests for provenance stamps (P5 / M7) — derived-phase staleness made REAL.

Everything is OFFLINE: an isolated tmp data root (via ``MAKE_MAGIC_DATA_DIR``)
backs the ``decks`` table in ``make_magic.duckdb``; no network, Airtable mocked.

The whole point of M7: staleness is STORED (freshness.assessment + last_sim,
keyed on ``version()``), not remembered — so a fresh ``DecksStore`` in a new
process reads the stamps back and derives the same tri-state (fresh|stale|absent).
Covers:

    - ``set_assessment`` stamps ``freshness.assessment = {version, at}`` and does
      NOT clobber the existing ``pulled_at`` (the W4 pull stamp coexists);
    - a later edit (version moves) makes the assessment stamp STALE;
    - ``set_last_sim`` writes ``last_sim = {result, deck_version, at}``, the result
      round-trips, and a later edit makes the sim stamp STALE;
    - tri-state ABSENT for a never-assessed / never-simmed deck;
    - CROSS-SESSION: a FRESH ``DecksStore()`` reads the stamps and derives state;
    - the ``stamp-sim`` CLI verb + ``list-decks --json`` states + ``get-deck``
      default-unchanged and ``--provenance`` block.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import store
from pipeline.collection import run as cli
from pipeline.contracts import Card, Deck, DeckCard
from pipeline.decks import DecksStore, version


class _StubResolver:
    def get_card(self, name: str) -> Card | None:
        return None


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at an isolated tmp data root; local backend, offline."""
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _StubResolver())
    return root


def _deck(name: str = 'Krenko Goblins') -> Deck:
    cards = [DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander'), DeckCard(name='Mountain', quantity=9)]
    for i in range(90):
        cards.append(DeckCard(name=f'Goblin {i}', quantity=1))
    return Deck(name=name, format='commander', strategy='go wide', cards=cards)


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr('sys.argv', ['collection', *argv])
    cli.main()


# --------------------------------------------------------------------------- #
# Assessment freshness stamp (merge, don't clobber pulled_at)
# --------------------------------------------------------------------------- #


def test_set_assessment_stamps_current_version(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    s.set_assessment('d1', 'plays fast; light on removal')

    row = s.get_row('d1')
    assert row is not None
    freshness = json.loads(row.freshness)
    assert freshness['assessment']['version'] == version(s.get('d1'))
    assert isinstance(freshness['assessment']['at'], str) and freshness['assessment']['at']


def test_set_assessment_merges_and_preserves_pulled_at(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    # A prior W4 pull stamp lives in freshness; the assessment stamp must MERGE.
    s.set_freshness('d1', {'pulled_at': '2026-08-04T00:00:00+00:00'})
    s.set_assessment('d1', 'reality synthesis')

    freshness = json.loads(s.get_row('d1').freshness)
    assert freshness['pulled_at'] == '2026-08-04T00:00:00+00:00'  # NOT clobbered
    assert 'assessment' in freshness


def test_later_edit_makes_assessment_stale(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    s.set_assessment('d1', 'synthesis')
    assert s.assessment_state('d1') == 'fresh'

    # A content edit (version moves) invalidates the stamped assessment.
    s.add_card('d1', DeckCard(name='Lightning Bolt', quantity=1))
    assert s.assessment_state('d1') == 'stale'


def test_assessment_absent_when_never_assessed(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    assert s.assessment_state('d1') == 'absent'


# --------------------------------------------------------------------------- #
# last_sim stamp (the thin stamp-sim hook)
# --------------------------------------------------------------------------- #


def test_set_last_sim_writes_result_and_version(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    s.set_last_sim('d1', result={'winrate': 0.42, 'games': 50})

    row = s.get_row('d1')
    last_sim = json.loads(row.last_sim)
    assert last_sim['result'] == {'winrate': 0.42, 'games': 50}
    assert last_sim['deck_version'] == version(s.get('d1'))
    assert isinstance(last_sim['at'], str) and last_sim['at']


def test_sim_result_roundtrips_and_later_edit_makes_stale(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    s.set_last_sim('d1', result={'winrate': 0.42})
    assert s.sim_state('d1') == 'fresh'
    assert json.loads(s.get_row('d1').last_sim)['result'] == {'winrate': 0.42}

    s.add_card('d1', DeckCard(name='Sol Ring', quantity=1))
    assert s.sim_state('d1') == 'stale'


def test_sim_absent_when_never_simmed(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    assert s.sim_state('d1') == 'absent'


# --------------------------------------------------------------------------- #
# CROSS-SESSION — the whole point of M7 (stamps are stored, not remembered)
# --------------------------------------------------------------------------- #


def test_cross_session_state_read_back_from_a_fresh_store(data_dir: Path) -> None:
    # Session 1: stamp assessment + sim, then leave one stale by editing after.
    s1 = DecksStore()
    s1.put(_deck(), deck_uuid='d1')
    s1.set_assessment('d1', 'synthesis')
    s1.set_last_sim('d1', result={'winrate': 0.5})
    # Edit AFTER the sim stamp -> sim goes stale, but assessment was re-stamped by
    # nothing since... actually add_card moves version, invalidating BOTH stamps.
    s1.add_card('d1', DeckCard(name='Goblin Chieftain', quantity=1))

    # Session 2: a brand-new store/process reads the persisted stamps back.
    s2 = DecksStore()
    assert s2.assessment_state('d1') == 'stale'
    assert s2.sim_state('d1') == 'stale'
    # And a re-assessment against the new content reads FRESH cross-session.
    s2.set_assessment('d1', 'updated synthesis')
    assert DecksStore().assessment_state('d1') == 'fresh'


def test_cross_session_absent_stays_absent(data_dir: Path) -> None:
    DecksStore().put(_deck(), deck_uuid='d1')
    assert DecksStore().assessment_state('d1') == 'absent'
    assert DecksStore().sim_state('d1') == 'absent'


# --------------------------------------------------------------------------- #
# CLI exposure — stamp-sim verb, list-decks --json, get-deck --provenance
# --------------------------------------------------------------------------- #


def test_stamp_sim_verb_registered() -> None:
    assert 'stamp-sim' in cli.VERBS


def test_stamp_sim_verb_writes_last_sim(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # Make an ephemeral draft so there is a resolvable local row (no source needed).
    s = DecksStore()
    s.create_ephemeral(_deck('Sim Target'))
    capsys.readouterr()

    _run(monkeypatch, 'stamp-sim', 'Sim Target', '--result', '{"winrate": 0.42, "games": 20}')
    capsys.readouterr()

    uuid = s.uuid_for_name('Sim Target')
    last_sim = json.loads(DecksStore().get_row(uuid).last_sim)
    assert last_sim['result'] == {'winrate': 0.42, 'games': 20}
    assert last_sim['deck_version'] == version(DecksStore().get(uuid))


def test_stamp_sim_verb_accepts_raw_string_result(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    s = DecksStore()
    s.create_ephemeral(_deck('Raw Sim'))
    capsys.readouterr()
    _run(monkeypatch, 'stamp-sim', 'Raw Sim', '--result', 'not json — a summary line')
    capsys.readouterr()
    uuid = s.uuid_for_name('Raw Sim')
    assert json.loads(DecksStore().get_row(uuid).last_sim)['result'] == 'not json — a summary line'


def test_list_decks_json_shows_provenance_states(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    s = DecksStore()
    s.create_ephemeral(_deck('Prov Draft'))
    uuid = s.uuid_for_name('Prov Draft')
    s.set_assessment(uuid, 'synthesis')  # fresh assessment, never simmed
    capsys.readouterr()

    _run(monkeypatch, 'list-decks', '--json')
    rows = json.loads(capsys.readouterr().out)
    draft = next(r for r in rows if r['name'] == 'Prov Draft')
    assert draft['assessment'] == 'fresh'
    assert draft['sim'] == 'absent'


def test_get_deck_default_output_unchanged(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    s = DecksStore()
    s.create_ephemeral(_deck('Plain Deck'))
    uuid = s.uuid_for_name('Plain Deck')
    s.set_assessment(uuid, 'synthesis')
    s.set_last_sim(uuid, result={'winrate': 0.5})
    capsys.readouterr()

    _run(monkeypatch, 'get-deck', 'Plain Deck')
    out = capsys.readouterr().out
    # DEFAULT output is byte-for-byte the deck JSON — no provenance leakage.
    parsed = json.loads(out)
    assert parsed == json.loads(DecksStore().get(uuid).model_dump_json(indent=2))
    assert 'provenance' not in parsed


def test_get_deck_provenance_flag_emits_block(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    s = DecksStore()
    s.create_ephemeral(_deck('Prov Deck'))
    uuid = s.uuid_for_name('Prov Deck')
    s.set_assessment(uuid, 'synthesis')
    s.set_last_sim(uuid, result={'winrate': 0.5})
    capsys.readouterr()

    _run(monkeypatch, 'get-deck', 'Prov Deck', '--provenance')
    out = json.loads(capsys.readouterr().out)
    assert 'deck' in out and 'provenance' in out
    prov = out['provenance']
    assert prov['assessment']['state'] == 'fresh'
    assert prov['assessment']['version'] == version(DecksStore().get(uuid))
    assert prov['last_sim']['state'] == 'fresh'
    assert prov['last_sim']['result'] == {'winrate': 0.5}
    assert prov['last_sim']['deck_version'] == version(DecksStore().get(uuid))
