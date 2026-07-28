"""Unit-test the repointed regression harness wiring (Phase 2.2).

The live ``check-cli`` gate against a real base is DEFERRED (no creds here). This
test proves the WIRING two ways, both offline:

    1. ``run_cli_equivalence_checks`` parses CLI stdout into a same-results
       FlowReport (``run_collection_cli`` stubbed — the subprocess is the seam).
    2. The collection CLI in Airtable mode drives the record adapter over an
       in-memory httpx transport double end-to-end (no network, no creds), so a
       ``list-*`` verb returns Airtable-sourced records — the exact path the
       harness shells out to.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline import config
from pipeline.collection import run as cli

_REGRESSION_PATH = Path(__file__).resolve().parents[2] / 'regression' / 'airtable_regression.py'


def _load_regression_module() -> Any:
    name = 'airtable_regression_undertest'
    spec = importlib.util.spec_from_file_location(name, _REGRESSION_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec so the module's dataclasses (Check/FlowReport) can
    # resolve their own annotations via sys.modules[cls.__module__].
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_CONTRACT = {
    'skills': {
        'managing-inventory': {'tables': {'Inventory Cards': {}, 'Decks': {}, 'Trades': {}}},
        'chasing-cards': {'tables': {'Chase Cards': {}, 'Decks': {}}},
    }
}


def _completed(stdout: str, returncode: int = 0, stderr: str = '') -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=['x'], returncode=returncode, stdout=stdout, stderr=stderr)


def test_equivalence_check_parses_cli_output(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = _load_regression_module()
    outputs = {
        'list-decks': 'Gruul Aggro\nMono Blue\n',
        'list-inventory': json.dumps([{'name': 'Sol Ring'}]),
        'list-chase': json.dumps([]),
        'list-trades': json.dumps([{'from_source': 'Library'}]),
    }
    monkeypatch.setattr(reg, 'run_collection_cli', lambda verb, *a, **k: _completed(outputs[verb]))
    report = reg.run_cli_equivalence_checks(_CONTRACT)
    assert report.status.value == 'PASS'
    # every covered domain's verb ran (Decks/Inventory/Trades/Chase).
    names = {c.name for c in report.checks}
    assert names == {'cli:list-decks', 'cli:list-inventory', 'cli:list-chase', 'cli:list-trades'}


def test_equivalence_check_fails_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = _load_regression_module()
    monkeypatch.setattr(
        reg,
        'run_collection_cli',
        lambda verb, *a, **k: _completed('', returncode=1, stderr='boom'),
    )
    report = reg.run_cli_equivalence_checks(_CONTRACT)
    assert report.status.value == 'FAIL'


def test_check_cli_skips_without_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DEFERRED live gate: no creds -> clear message + clean exit (no crash)."""
    reg = _load_regression_module()
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)
    with pytest.raises(Exception) as ei:  # typer.Exit
        reg.check_cli()
    # typer.Exit carries exit_code 0 for a clean skip.
    assert getattr(ei.value, 'exit_code', getattr(ei.value, 'code', None)) == 0


# --------------------------------------------------------------------------- #
# End-to-end wiring over an in-memory transport double: the CLI in Airtable mode
# drives the record adapter (the exact path check-cli shells out to).
# --------------------------------------------------------------------------- #


def test_collection_cli_airtable_mode_lists_records_via_transport_double(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from tests.test_airtable_collection import FakeAirtable, _inv_row

    fake = FakeAirtable({'tblCards': [_inv_row('recSol', 'Sol Ring')]})

    def _fake_store(resolver: Any, *, writes_enabled: bool = False) -> Any:
        from pipeline.collection.adapters.airtable_collection import AirtableCollectionStore

        client = httpx.Client(transport=httpx.MockTransport(fake.handler))
        return AirtableCollectionStore.from_settings('fake-token', writes_enabled=writes_enabled, client=client)

    config.get_settings.cache_clear()
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'airtable')
    monkeypatch.setattr(cli, 'get_store', _fake_store)
    monkeypatch.setattr(cli, '_load_resolver', lambda: None)
    monkeypatch.setattr('sys.argv', ['collection', 'list-inventory'])

    cli.main()
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]['name'] == 'Sol Ring'
    assert rows[0]['owned'] == 3
