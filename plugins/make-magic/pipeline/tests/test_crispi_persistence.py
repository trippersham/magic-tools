"""CRISPI persistence — the derived-output stamp on a deck row (Phase 7).

The exact mirror of the ``last_sim`` / ``stamp-sim`` precedent
(``test_decks_provenance.py``). CRISPI is a DERIVED output like sim: it is stamped
against the deck's current ``version()`` (so a later content edit makes it
derivably ``stale``), stored on a local-only ``crispi`` DuckDB column as
``{result, deck_version, at}``, and is NOT part of the content ``version()`` hash.

Everything is OFFLINE: an isolated tmp data root (via ``MAKE_MAGIC_DATA_DIR``)
backs the ``decks`` table in ``make_magic.duckdb``; no network. Covers:

    - ``set_crispi`` writes ``crispi = {result, deck_version, at}``, the structured
      ``CrispiResult`` result round-trips, and a later edit makes it STALE;
    - tri-state ABSENT for a never-scored deck; cross-session read-back;
    - the ``stamp-crispi`` CLI verb (registered) writes it, accepts a raw string,
      and ``get-deck --field crispi`` reads it back;
    - the content ``version()`` is UNCHANGED by a CRISPI stamp (derived, not content);
    - commit-through resolution: the stamp lands on the local row for a SYNCED
      source deck (pull-current) and for an EPHEMERAL draft alike;
    - Airtable-tolerant: crispi is a local-only column (never synced), so a re-pull
      from an Airtable source that lacks the column neither wipes nor crashes it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _decks_helpers import commander_deck, source_store

from pipeline import store
from pipeline.collection import run as cli
from pipeline.contracts import Card, CrispiResult, Deck, DeckCard
from pipeline.decks import DecksStore, version
from pipeline.decks.access import deck_access


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


def _axis(value: float, why: str) -> dict:
    return {'value': value, 'rationale': why, 'cited_cards': []}


def _crispi_result() -> dict:
    """A valid structured ``CrispiResult`` dict — what the skill stamps verbatim."""
    result = CrispiResult(
        consistency=_axis(6.0, 'draw + tutors'),
        interaction=_axis(7.0, 'removal density'),
        speed=_axis(7.0, 'fundamental turn 7'),
        resilience=_axis(5.0, 'combat plan'),
        performance_index=6.25,
        bracket={'bracket': 3, 'triggers': ['Speed 6+']},
        inputs={'fundamental_turn': 7.0, 'commander_dependence': 'med'},
        computed_at='2026-08-14T00:00:00+00:00',
    )
    return result.model_dump(mode='json')


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr('sys.argv', ['collection', *argv])
    cli.main()


# --------------------------------------------------------------------------- #
# Store: set_crispi writes {result, deck_version, at}; round-trip + staleness.
# --------------------------------------------------------------------------- #


def test_set_crispi_writes_result_and_version(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    result = _crispi_result()
    s.set_crispi('d1', result=result)

    row = s.get_row('d1')
    crispi = json.loads(row.crispi)
    assert crispi['result'] == result
    assert crispi['deck_version'] == version(s.get('d1'))
    assert isinstance(crispi['at'], str) and crispi['at']


def test_crispi_result_roundtrips_as_a_valid_crispiresult(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    s.set_crispi('d1', result=_crispi_result())

    stored = json.loads(s.get_row('d1').crispi)['result']
    # The stored blob re-validates as a CrispiResult (the structured shape survives).
    revalidated = CrispiResult.model_validate(stored)
    assert revalidated.performance_index == 6.25
    assert revalidated.bracket is not None and revalidated.bracket.bracket == 3


def test_later_edit_makes_crispi_stale(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    s.set_crispi('d1', result=_crispi_result())
    assert s.crispi_state('d1') == 'fresh'

    # A content edit (version moves) invalidates the stamped CRISPI.
    s.add_card('d1', DeckCard(name='Sol Ring', quantity=1))
    assert s.crispi_state('d1') == 'stale'


def test_crispi_absent_when_never_scored(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    assert s.crispi_state('d1') == 'absent'


def test_set_crispi_raises_when_deck_absent(data_dir: Path) -> None:
    from pipeline.decks import DecksError

    s = DecksStore()
    with pytest.raises(DecksError):
        s.set_crispi('nope', result=_crispi_result())


# --------------------------------------------------------------------------- #
# The content version() is UNCHANGED by a CRISPI stamp (derived, not content).
# --------------------------------------------------------------------------- #


def test_crispi_stamp_does_not_change_content_version(data_dir: Path) -> None:
    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    before = version(s.get('d1'))
    s.set_crispi('d1', result=_crispi_result())
    after = version(s.get('d1'))
    assert before == after, 'stamping CRISPI must not move the content version hash'


# --------------------------------------------------------------------------- #
# Cross-session — the stamp is STORED, not remembered.
# --------------------------------------------------------------------------- #


def test_cross_session_crispi_state_read_back(data_dir: Path) -> None:
    s1 = DecksStore()
    s1.put(_deck(), deck_uuid='d1')
    s1.set_crispi('d1', result=_crispi_result())
    s1.add_card('d1', DeckCard(name='Goblin Chieftain', quantity=1))  # -> stale

    s2 = DecksStore()  # a brand-new store/process
    assert s2.crispi_state('d1') == 'stale'
    s2.set_crispi('d1', result=_crispi_result())  # re-score against new content
    assert DecksStore().crispi_state('d1') == 'fresh'


# --------------------------------------------------------------------------- #
# CLI — stamp-crispi verb + get-deck --field crispi + --provenance.
# --------------------------------------------------------------------------- #


def test_stamp_crispi_verb_registered() -> None:
    assert 'stamp-crispi' in cli.VERBS


def test_stamp_crispi_verb_writes_crispi(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    s = DecksStore()
    s.create_ephemeral(_deck('Crispi Target'))
    capsys.readouterr()

    _run(monkeypatch, 'stamp-crispi', 'Crispi Target', '--result', json.dumps(_crispi_result()))
    capsys.readouterr()

    uuid = s.uuid_for_name('Crispi Target')
    crispi = json.loads(DecksStore().get_row(uuid).crispi)
    assert crispi['result'] == _crispi_result()
    assert crispi['deck_version'] == version(DecksStore().get(uuid))


def test_stamp_crispi_verb_reads_result_from_stdin(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # `--result -` reads the JSON from stdin — the robust skill path (a real
    # CrispiResult carries apostrophes that break inline shell quoting).
    import io

    s = DecksStore()
    s.create_ephemeral(_deck('Stdin Crispi'))
    capsys.readouterr()

    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(_crispi_result())))
    _run(monkeypatch, 'stamp-crispi', 'Stdin Crispi', '--result', '-')
    capsys.readouterr()

    uuid = s.uuid_for_name('Stdin Crispi')
    assert json.loads(DecksStore().get_row(uuid).crispi)['result'] == _crispi_result()


def test_stamp_crispi_verb_accepts_raw_string_result(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    s = DecksStore()
    s.create_ephemeral(_deck('Raw Crispi'))
    capsys.readouterr()
    _run(monkeypatch, 'stamp-crispi', 'Raw Crispi', '--result', 'CRISPI 6.25 · S7/C6/I7/R5 · B3')
    capsys.readouterr()
    uuid = s.uuid_for_name('Raw Crispi')
    assert json.loads(DecksStore().get_row(uuid).crispi)['result'] == 'CRISPI 6.25 · S7/C6/I7/R5 · B3'


def test_get_deck_field_crispi_reads_it_back(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    s = DecksStore()
    s.create_ephemeral(_deck('Field Crispi'))
    uuid = s.uuid_for_name('Field Crispi')
    s.set_crispi(uuid, result=_crispi_result())
    capsys.readouterr()

    _run(monkeypatch, 'get-deck', 'Field Crispi', '--field', 'crispi')
    out = capsys.readouterr().out
    blob = json.loads(out)
    assert blob['result'] == _crispi_result()
    assert blob['deck_version'] == version(DecksStore().get(uuid))


def test_get_deck_field_crispi_empty_when_never_scored(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    s = DecksStore()
    s.create_ephemeral(_deck('No Crispi'))
    capsys.readouterr()
    _run(monkeypatch, 'get-deck', 'No Crispi', '--field', 'crispi')
    assert capsys.readouterr().out.strip() == ''


def test_get_deck_provenance_includes_crispi(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    s = DecksStore()
    s.create_ephemeral(_deck('Prov Crispi'))
    uuid = s.uuid_for_name('Prov Crispi')
    s.set_crispi(uuid, result=_crispi_result())
    capsys.readouterr()

    _run(monkeypatch, 'get-deck', 'Prov Crispi', '--provenance')
    prov = json.loads(capsys.readouterr().out)['provenance']
    assert prov['crispi']['state'] == 'fresh'
    assert prov['crispi']['result'] == _crispi_result()
    assert prov['crispi']['deck_version'] == version(DecksStore().get(uuid))


def test_get_deck_default_output_unchanged_by_crispi(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    s = DecksStore()
    s.create_ephemeral(_deck('Plain Crispi'))
    uuid = s.uuid_for_name('Plain Crispi')
    s.set_crispi(uuid, result=_crispi_result())
    capsys.readouterr()

    _run(monkeypatch, 'get-deck', 'Plain Crispi')
    parsed = json.loads(capsys.readouterr().out)
    # DEFAULT output is byte-for-byte the deck JSON — no crispi leakage.
    assert parsed == json.loads(DecksStore().get(uuid).model_dump_json(indent=2))
    assert 'crispi' not in parsed


# --------------------------------------------------------------------------- #
# Commit-through: SYNCED source deck (pull-current) vs EPHEMERAL draft.
# --------------------------------------------------------------------------- #


def test_stamp_crispi_on_synced_deck_lands_on_local_row(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A synced source deck is pulled-current so there is a row to stamp (mirror sim)."""
    driver = source_store(data_dir)
    driver.save_deck(commander_deck('Synced Crispi'), allow_shrink=False)
    capsys.readouterr()

    # No local row yet — the verb must pull-current the synced source first.
    _run(monkeypatch, 'stamp-crispi', 'Synced Crispi', '--result', json.dumps(_crispi_result()))
    capsys.readouterr()

    decks = DecksStore()
    uuid = decks.uuid_for_name('Synced Crispi')
    assert uuid is not None
    assert decks.crispi_state(uuid) == 'fresh'
    assert json.loads(decks.get_row(uuid).crispi)['result'] == _crispi_result()


def test_stamp_crispi_on_ephemeral_draft_stays_local(data_dir: Path) -> None:
    driver = source_store(data_dir)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)
    uuid = decks.create_ephemeral(commander_deck('Ephemeral Crispi'))

    access.set_crispi('Ephemeral Crispi', _crispi_result())
    assert decks.crispi_state(uuid) == 'fresh'
    # Still an ephemeral draft (no source push happened — a stamp is bookkeeping only).
    assert decks.get_row(uuid).sync_status == 'ephemeral'


# --------------------------------------------------------------------------- #
# Airtable-tolerant: crispi is a LOCAL-ONLY column (never synced), so a re-pull
# from a source that lacks the column neither wipes nor crashes it — the mirror
# of test_re_pull_preserves_assessment_stamp (crispi is like last_sim, off-record).
# --------------------------------------------------------------------------- #


def test_re_pull_preserves_crispi_stamp(data_dir: Path) -> None:
    driver = source_store(data_dir)
    decks = DecksStore()
    access = deck_access(driver, decks=decks)

    driver.save_deck(commander_deck('Persist Crispi'), allow_shrink=False)
    access.read_deck('Persist Crispi')
    deck_uuid = access.resolve('Persist Crispi')
    access.set_crispi('Persist Crispi', _crispi_result())
    assert decks.crispi_state(deck_uuid) == 'fresh'

    # Force a re-pull (bind by ref); the source carries no crispi column, so the
    # local-only crispi stamp must SURVIVE and the pull must not crash.
    access.pull('Persist Crispi')
    assert decks.crispi_state(deck_uuid) == 'fresh', 're-pull wiped the crispi stamp'
    assert json.loads(decks.get_row(deck_uuid).crispi)['result'] == _crispi_result()
