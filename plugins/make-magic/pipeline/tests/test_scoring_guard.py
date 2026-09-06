"""Cold-start scoring-guard tests (Phase 1 + Phase 2).

The seam this locks: a scoring/summary verb (``factsheet`` / ``crispi``) must
NEVER emit all-zeros against un-enriched cards. When the card lake is absent or a
seed stub, or the deck's cards stay name-only after read-time enrichment, the verb
REFUSES loudly (exit nonzero) with remediation text that names
``collection hydrate-lake`` verbatim — the loud-refusal-over-silent-zeros rule.

Phase 2 adds the public ``hydrate-lake`` verb; its wiring/idempotence is unit
tested against monkeypatched source/build callables (no network).

OFFLINE: a tmp ``MAKE_MAGIC_DATA_DIR`` + local backend; the lake is written
directly (or its status monkeypatched) so nothing hits Scryfall.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import store
from pipeline.collection import run as cli
from pipeline.contracts import Card


class _EnrichingResolver:
    """A resolver that fully enriches every name (a stand-in for a hydrated lake)."""

    def get_card(self, name: str) -> Card | None:
        import uuid

        return Card(name=name, oracle_id=str(uuid.uuid5(uuid.NAMESPACE_OID, name)))


class _NameOnlyResolver:
    """A resolver that resolves nothing — every card stays name-only (oracle_id None)."""

    def get_card(self, name: str) -> Card | None:
        return None


def _deck_factsheet_mod():
    """Import the sibling ``scripts/deck_factsheet.py`` (the otag read surface)."""
    import sys

    scripts_dir = Path(__file__).resolve().parents[2] / 'scripts'
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import deck_factsheet  # type: ignore[import-not-found]

    return deck_factsheet


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr('sys.argv', ['collection', *argv])
    cli.main()


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    return root


def _import_basics_deck(monkeypatch: pytest.MonkeyPatch, n: int = 100) -> None:
    """Import a plaintext basics list as an ephemeral draft (no lake needed to land it)."""
    text = f'{n} Swamp\n'
    monkeypatch.setattr('sys.stdin', __import__('io').StringIO(text))
    _run(monkeypatch, 'import-deck', '-', '--name', 'Cold Start', '--source', 'plaintext')


# --------------------------------------------------------------------------- #
# T1.3 — lake status: absent / stub / ready (row-count floor)
# --------------------------------------------------------------------------- #


def test_lake_status_absent_stub_ready(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline.collection import resolver as R

    assert R.lake_status() == 'absent'

    with store.connect() as conn:
        store.write_parquet(conn, "SELECT 1 AS oracle_id, 'Krenko, Mob Boss' AS name", 'raw', 'oracle_cards')
    assert R.lake_status() == 'stub'

    with store.connect() as conn:
        store.write_parquet(
            conn,
            "SELECT i AS oracle_id, 'Card ' || i AS name FROM range(2000) t(i)",
            'raw',
            'oracle_cards',
        )
    assert R.lake_status() == 'ready'


# --------------------------------------------------------------------------- #
# T1.1 — refusal in factsheet / crispi when the lake is absent
# --------------------------------------------------------------------------- #


def test_factsheet_refuses_when_lake_absent(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _NameOnlyResolver())
    _import_basics_deck(monkeypatch)
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 'factsheet', 'Cold Start')
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert 'collection hydrate-lake' in err


def test_crispi_refuses_when_lake_absent(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _NameOnlyResolver())
    _import_basics_deck(monkeypatch)
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 'crispi', 'Cold Start', '--commander-dependence', 'med')
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert 'collection hydrate-lake' in err


# --------------------------------------------------------------------------- #
# T1.3 — refusal against a stub lake (never zeros)
# --------------------------------------------------------------------------- #


def test_factsheet_refuses_against_stub_lake(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _NameOnlyResolver())
    _import_basics_deck(monkeypatch)
    # A seed stub (1 row) is present but must NOT count as a real lake.
    with store.connect() as conn:
        store.write_parquet(conn, "SELECT 1 AS oracle_id, 'Krenko, Mob Boss' AS name", 'raw', 'oracle_cards')
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 'factsheet', 'Cold Start')
    assert exc.value.code != 0
    assert 'collection hydrate-lake' in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# T1.2 — read-path enrichment: a hydrated lake makes factsheet score, no promote
# --------------------------------------------------------------------------- #


def test_factsheet_scores_when_lake_ready(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import json

    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _EnrichingResolver())
    monkeypatch.setattr('pipeline.collection.resolver.lake_status', lambda: 'ready')
    _import_basics_deck(monkeypatch)
    capsys.readouterr()

    _run(monkeypatch, 'factsheet', 'Cold Start')
    report = json.loads(capsys.readouterr().out)
    assert report['deck'] == 'Cold Start'
    # Every name enriched -> no missing, real coverage (not the all-zeros seam).
    assert report.get('missing') == []


# --------------------------------------------------------------------------- #
# T2.1 — hydrate-lake verb wiring + idempotence (monkeypatched, no network)
# --------------------------------------------------------------------------- #


def test_hydrate_lake_wires_source_then_build(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    calls: list[str] = []

    def fake_sync(*, client=None, force=False, max_cards=None):
        calls.append(f'sync(force={force},max_cards={max_cards})')
        return Path('/tmp/oracle_cards.parquet')

    def fake_run(*, ingest=True):
        calls.append(f'build(ingest={ingest})')
        return {'card_otag': '/tmp/card_otag.parquet'}

    monkeypatch.setattr('pipeline.sources.scryfall_bulk.sync', fake_sync)
    monkeypatch.setattr('pipeline.transforms.build.run', fake_run)

    _run(monkeypatch, 'hydrate-lake')
    # Source pull happens BEFORE the mart build.
    assert calls == ['sync(force=False,max_cards=None)', 'build(ingest=True)']

    # Idempotent re-run: the verb calls the same (cursor-gated / rebuild) path again
    # without error — the no-clobber semantics live in sync/build, not the verb.
    calls.clear()
    capsys.readouterr()
    _run(monkeypatch, 'hydrate-lake')
    assert calls == ['sync(force=False,max_cards=None)', 'build(ingest=True)']


def test_hydrate_lake_threads_force_and_max_cards(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    seen: dict[str, object] = {}

    def fake_sync(*, client=None, force=False, max_cards=None):
        seen['force'] = force
        seen['max_cards'] = max_cards
        return Path('/tmp/oracle_cards.parquet')

    monkeypatch.setattr('pipeline.sources.scryfall_bulk.sync', fake_sync)
    monkeypatch.setattr('pipeline.transforms.build.run', lambda *, ingest=True: {})

    _run(monkeypatch, 'hydrate-lake', '--force', '--max-cards', '500')
    assert seen == {'force': True, 'max_cards': 500}


# --------------------------------------------------------------------------- #
# T2.2 — onboard prints the hydrate-lake next step when the lake is absent
# --------------------------------------------------------------------------- #


def test_onboard_local_hints_hydrate_lake_when_absent(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run(monkeypatch, 'onboard', '--backend', 'local')
    out = capsys.readouterr().out
    assert 'collection hydrate-lake' in out


def test_onboard_local_no_hint_when_lake_ready(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr('pipeline.collection.resolver.lake_status', lambda: 'ready')
    _run(monkeypatch, 'onboard', '--backend', 'local')
    out = capsys.readouterr().out
    assert 'hydrate-lake' not in out


# --------------------------------------------------------------------------- #
# Helpers for the Phase-3 / rider tests
# --------------------------------------------------------------------------- #


def _import_commander_format_no_commander(monkeypatch: pytest.MonkeyPatch) -> None:
    """Import a commander-format list (a `#`-header with commander metadata) with 0 commanders."""
    text = '# Cold Start — commanders=1 total=100\n' + ('1 Swamp\n' * 100)
    monkeypatch.setattr('sys.stdin', __import__('io').StringIO(text))
    _run(monkeypatch, 'import-deck', '-', '--source', 'plaintext')


# --------------------------------------------------------------------------- #
# T3.2 — commander-format deck with 0 commanders: importer warns, scoring refuses
# --------------------------------------------------------------------------- #


def test_import_commander_format_no_commander_warns(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _import_commander_format_no_commander(monkeypatch)
    err = capsys.readouterr().err
    assert '--commander' in err
    assert 'Commander-format' in err or 'commander' in err.lower()


def test_scoring_refuses_commander_format_zero_commanders(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _EnrichingResolver())
    monkeypatch.setattr('pipeline.collection.resolver.lake_status', lambda: 'ready')
    _import_commander_format_no_commander(monkeypatch)
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 'factsheet', 'Cold Start')
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert '0 commanders' in err
    assert '--commander' in err


def test_import_commander_flag_satisfies_guard(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _EnrichingResolver())
    monkeypatch.setattr('pipeline.collection.resolver.lake_status', lambda: 'ready')
    text = '# Cold Start — commanders=1 total=100\n1 Krenko, Mob Boss\n' + ('1 Swamp\n' * 99)
    monkeypatch.setattr('sys.stdin', __import__('io').StringIO(text))
    _run(monkeypatch, 'import-deck', '-', '--source', 'plaintext', '--commander', 'Krenko, Mob Boss')
    capsys.readouterr()

    # A commander is present -> the commander-format guard does not trip; factsheet scores.
    import json

    _run(monkeypatch, 'factsheet', 'Cold Start')
    report = json.loads(capsys.readouterr().out)
    assert report['deck'] == 'Cold Start'


# --------------------------------------------------------------------------- #
# R1 — low-coverage refusal (ready lake, deck stays name-only past tolerance)
# --------------------------------------------------------------------------- #


def test_scoring_refuses_low_coverage_ready_lake(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # The lake is READY, but the resolver enriches nothing (every card stays
    # name-only) -> resolved/total = 0 < _MIN_SCORING_COVERAGE -> loud refusal.
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _NameOnlyResolver())
    monkeypatch.setattr('pipeline.collection.resolver.lake_status', lambda: 'ready')
    _import_basics_deck(monkeypatch)
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 'factsheet', 'Cold Start')
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert 'collection hydrate-lake' in err
    assert '0/100' in err or 'name-only' in err


# --------------------------------------------------------------------------- #
# R2 — crispi refuses when oracle_cards is ready but the card_otag mart is absent
# --------------------------------------------------------------------------- #


def test_crispi_refuses_when_otag_closure_unavailable(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # #53: crispi guards through the SAME closure the factsheet reads. A None closure
    # (otag layer entirely unavailable) -> loud refusal naming hydrate-lake.
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _EnrichingResolver())
    monkeypatch.setattr('pipeline.collection.resolver.lake_status', lambda: 'ready')
    _deck_factsheet_mod()
    monkeypatch.setattr('deck_factsheet._load_card_otag', lambda: None)
    _import_basics_deck(monkeypatch)
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 'crispi', 'Cold Start', '--commander-dependence', 'med')
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert 'oracle-tag' in err
    assert 'collection hydrate-lake' in err


def test_crispi_refuses_when_otag_coverage_snapshot_degraded(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # #53: a closure that tags too few of the deck's nonlands (snapshot-degraded,
    # below OTAG_COVERAGE_FLOOR) -> refuse rather than score blind.
    import uuid

    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _EnrichingResolver())
    monkeypatch.setattr('pipeline.collection.resolver.lake_status', lambda: 'ready')
    # Import 10 distinct NONLAND cards (the enriching resolver leaves type_line empty).
    names = [f'Spell {i}' for i in range(10)]
    text = ''.join(f'1 {n}\n' for n in names)
    monkeypatch.setattr('sys.stdin', __import__('io').StringIO(text))
    _run(monkeypatch, 'import-deck', '-', '--name', 'Cold Start', '--source', 'plaintext')
    # Closure tags only 2/10 -> 20% coverage, below the 50% floor.
    tagged = {str(uuid.uuid5(uuid.NAMESPACE_OID, n)): {'ramp'} for n in names[:2]}
    _deck_factsheet_mod()
    monkeypatch.setattr('deck_factsheet._load_card_otag', lambda: tagged)
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 'crispi', 'Cold Start', '--commander-dependence', 'med')
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert 'snapshot-degraded' in err
    assert 'collection hydrate-lake' in err


def test_crispi_scores_degraded_loud_on_fresh_set(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # The fresh-set case: the closure is fully HYDRATED globally (tens of thousands of
    # tagged oracle_ids) but knows NONE of this deck's cards — a just-released set the
    # upstream tagger has not reached yet. CRISPI must SCORE (not refuse), and the
    # output must carry an un-missable degradation marker (top-level block + amended
    # otag-driven axis rationales) with a one-line stderr warning.
    import json
    import uuid

    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _EnrichingResolver())
    monkeypatch.setattr('pipeline.collection.resolver.lake_status', lambda: 'ready')
    names = [f'Spell {i}' for i in range(10)]
    text = ''.join(f'1 {n}\n' for n in names)
    monkeypatch.setattr('sys.stdin', __import__('io').StringIO(text))
    _run(monkeypatch, 'import-deck', '-', '--name', 'Fresh Set', '--source', 'plaintext')
    # A big, healthy closure (>= the hydrated-size floor) that covers NONE of the deck's
    # oracle_ids -> global-healthy, deck-coverage 0% -> score degraded-loud.
    big = {str(uuid.uuid5(uuid.NAMESPACE_OID, f'Other {i}')): {'ramp'} for i in range(12000)}
    _deck_factsheet_mod()
    monkeypatch.setattr('deck_factsheet._load_card_otag', lambda: big)
    capsys.readouterr()

    # An explicit fundamental turn avoids the Speed-N/A escape hatch for a wincon-less deck.
    _run(monkeypatch, 'crispi', 'Fresh Set', '--commander-dependence', 'med', '--fundamental-turn', '4')
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    # Un-missable JSON degradation block.
    assert report['otag_degraded'] is True
    assert report['otag_coverage'] == 0.0
    # Each otag-driven axis rationale notes the limited signal.
    for axis in ('interaction', 'resilience', 'consistency'):
        assert 'otag signal limited' in report[axis]['rationale']
    # One-line stderr warning.
    assert 'just-released set' in captured.err
    assert 'under-read' in captured.err
    # It really SCORED (not a refusal).
    assert 'collection hydrate-lake' not in captured.err
    assert isinstance(report['performance_index'], (int, float))


def test_factsheet_still_runs_when_otag_closure_unavailable(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # factsheet degrades to structured-only (its own marker) — it must NOT be
    # collateral-refused by the crispi-scoped otag guard.
    import json

    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _EnrichingResolver())
    monkeypatch.setattr('pipeline.collection.resolver.lake_status', lambda: 'ready')
    _deck_factsheet_mod()
    monkeypatch.setattr('deck_factsheet._load_card_otag', lambda: None)
    _import_basics_deck(monkeypatch)
    capsys.readouterr()

    _run(monkeypatch, 'factsheet', 'Cold Start')
    report = json.loads(capsys.readouterr().out)
    assert report['deck'] == 'Cold Start'


# --------------------------------------------------------------------------- #
# R3 — hydrate-lake --max-cards help notes the stub-floor interaction
# --------------------------------------------------------------------------- #


def test_hydrate_lake_help_notes_stub_floor(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from pipeline.collection.resolver import LAKE_STUB_FLOOR

    with pytest.raises(SystemExit):
        _run(monkeypatch, 'hydrate-lake', '-h')
    out = capsys.readouterr().out
    assert str(LAKE_STUB_FLOOR) in out
    assert 'stub' in out.lower()


# --------------------------------------------------------------------------- #
# T3.3 — re-import hygiene: content-identical note, no wedge
# --------------------------------------------------------------------------- #


def test_reimport_identical_list_notes_duplicate(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    text = '1 Sol Ring\n1 Arcane Signet\n'

    monkeypatch.setattr('sys.stdin', __import__('io').StringIO(text))
    _run(monkeypatch, 'import-deck', '-', '--name', 'Dup Deck', '--source', 'plaintext')
    capsys.readouterr()

    monkeypatch.setattr('sys.stdin', __import__('io').StringIO(text))
    _run(monkeypatch, 'import-deck', '-', '--name', 'Dup Deck', '--source', 'plaintext')
    err = capsys.readouterr().err
    assert 'content-identical' in err
    assert '--id' in err


# --------------------------------------------------------------------------- #
# #53 F3 — declared header total=N vs actual imported count
# --------------------------------------------------------------------------- #


def test_import_warns_on_header_total_mismatch(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # A `#`-header declares total=100 but only 99 cards are pasted -> loud stderr
    # warning naming BOTH numbers; the import still succeeds.
    text = '# Truncated — total=100\n' + ('1 Swamp\n' * 99)
    monkeypatch.setattr('sys.stdin', __import__('io').StringIO(text))
    _run(monkeypatch, 'import-deck', '-', '--name', 'Truncated', '--source', 'plaintext')
    captured = capsys.readouterr()
    assert 'total=100' in captured.err
    assert '99 cards' in captured.err
    assert 'Imported' in captured.out  # import still succeeded


def test_import_silent_when_header_total_matches(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    text = '# Exact — total=100\n' + ('1 Swamp\n' * 100)
    monkeypatch.setattr('sys.stdin', __import__('io').StringIO(text))
    _run(monkeypatch, 'import-deck', '-', '--name', 'Exact', '--source', 'plaintext')
    captured = capsys.readouterr()
    assert 'mismatch' not in captured.err


# --------------------------------------------------------------------------- #
# #53 F4 — --fundamental-turn range validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize('bad', ['-1', '0', '31'])
def test_crispi_rejects_out_of_range_fundamental_turn(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, bad: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 'crispi', 'Whatever', '--commander-dependence', 'med', '--fundamental-turn', bad)
    assert exc.value.code != 0
    assert 'range' in capsys.readouterr().err.lower()


@pytest.mark.parametrize('good', ['0.5', '8', '8.5'])
def test_fundamental_turn_arg_accepts_in_range(good: str) -> None:
    # The argparse `type` accepts sane values (unit-level: no deck needed).
    assert cli._fundamental_turn_arg(good) == float(good)


def test_ambiguous_name_message_names_disambiguation_options(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    text = '1 Sol Ring\n1 Arcane Signet\n'
    for _ in range(2):
        monkeypatch.setattr('sys.stdin', __import__('io').StringIO(text))
        _run(monkeypatch, 'import-deck', '-', '--name', 'Dup Deck', '--source', 'plaintext')
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 'get-deck', 'Dup Deck')
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert 'ambiguous' in err
    assert '--id' in err
    assert 'archive-deck' in err
