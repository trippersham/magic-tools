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
    monkeypatch.setattr('pipeline.collection.resolver.otag_mart_available', lambda: True)
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


def test_crispi_refuses_when_otag_mart_absent(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _EnrichingResolver())
    monkeypatch.setattr('pipeline.collection.resolver.lake_status', lambda: 'ready')
    monkeypatch.setattr('pipeline.collection.resolver.otag_mart_available', lambda: False)
    _import_basics_deck(monkeypatch)
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, 'crispi', 'Cold Start', '--commander-dependence', 'med')
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert 'card_otag' in err
    assert 'collection hydrate-lake' in err


def test_factsheet_still_runs_when_otag_mart_absent(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # factsheet degrades to structured-only (its own marker) — it must NOT be
    # collateral-refused by the crispi-scoped otag guard.
    import json

    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', lambda: _EnrichingResolver())
    monkeypatch.setattr('pipeline.collection.resolver.lake_status', lambda: 'ready')
    monkeypatch.setattr('pipeline.collection.resolver.otag_mart_available', lambda: False)
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
