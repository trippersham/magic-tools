#!/usr/bin/env -S uv run --python 3.12 --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "typer",
# ]
# [tool.uv]
# exclude-newer = "2026-06-08T00:00:00Z"
# ///
"""
Airtable happy-path regression harness (Phase 0).

PROVES that the Airtable-as-source-of-truth workflow spanning three skills
(managing-inventory, building-decks, chasing-cards) does not degrade as a new
data engine is built alongside it. It reads a committed *golden contract*
(golden_contract.json) enumerating the tables + fields + write ops each skill
depends on, then asserts that surface still exists in the live base.

Standalone PEP-723 uv script — its own deps, no dependency on the pipeline/
package. Talks to Airtable via the REST API directly (mirrors how the bundled
Airtable MCP server authenticates: an Airtable Personal Access Token sent as a
Bearer token). The Airtable IDENTITY is env-driven: the base id comes from
AIRTABLE_BASE_ID (default = the turnkey base) and every table is addressed by
NAME — the harness resolves the live per-base ``tbl…`` ids by NAME at runtime via
the meta API (``GET /v0/meta/bases/{base}/tables``), mirroring
``pipeline/config.py``'s ``AirtableResolver`` (re-implemented inline, GET-only +
cached, because this standalone script cannot import the pipeline package). So a
different Airtable instance runs this suite by exporting AIRTABLE_BASE_ID (and, if
its tables are named differently, the AIRTABLE_*_TABLE overrides) — no contract
edit required.

Modes:
    --read-only   Reads + golden-contract assertions only. SAFE against a
                  read-only token. Never mutates anything.
    --full        Adds a self-cleaning scratch write round-trip
                  (create -> read-back -> update -> verify -> DELETE) in a
                  designated leaf table. Cleans up even on failure. Requires a
                  write-capable token against the real base.

Usage:
    AIRTABLE_API_KEY=<pat> uv run --script airtable_regression.py --read-only
    AIRTABLE_API_KEY=<pat> uv run --script airtable_regression.py --full

    # Point at a different base/token combo (e.g. read-only mechanics smoke):
    AIRTABLE_API_KEY=<pat> uv run --script airtable_regression.py \
        --read-only --smoke-any-base

    # Discover what bases/tables a token can see (plumbing check):
    AIRTABLE_API_KEY=<pat> uv run --script airtable_regression.py list-bases

Overrides (env or flag) for the contract's defaults:
    --base-id / AIRTABLE_BASE_ID       base id (default = the turnkey base)
    --contract PATH   (defaults to golden_contract.json next to this script)
    AIRTABLE_CARDS_TABLE / _DECKS_TABLE / _TRADES_TABLE / _CHASE_TABLE
        Optional per-table NAME overrides (mirrors the pipeline's env vars) for a
        base whose tables are named differently from the contract's live names.

Exit code: 0 if every enabled check PASSes, 1 otherwise.

Maintenance:
    uvx ruff format airtable_regression.py
    uvx ruff check airtable_regression.py
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

import httpx
import typer

AIRTABLE_BASE_URL = 'https://api.airtable.com'
DEFAULT_CONTRACT_PATH = Path(__file__).with_name('golden_contract.json')

app = typer.Typer(add_completion=False, no_args_is_help=False)


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #


class Status(StrEnum):
    PASS = 'PASS'
    FAIL = 'FAIL'
    SKIP = 'SKIP'


@dataclass
class Check:
    name: str
    status: Status
    detail: str = ''


@dataclass
class FlowReport:
    """Per-skill-flow report."""

    skill: str
    checks: list[Check] = field(default_factory=list)

    @property
    def status(self) -> Status:
        if any(c.status is Status.FAIL for c in self.checks):
            return Status.FAIL
        if self.checks and all(c.status is Status.SKIP for c in self.checks):
            return Status.SKIP
        return Status.PASS

    def add(self, name: str, status: Status, detail: str = '') -> None:
        self.checks.append(Check(name=name, status=status, detail=detail))


# --------------------------------------------------------------------------- #
# Airtable REST client (mirrors the MCP server's auth + endpoints)
# --------------------------------------------------------------------------- #


class AirtableError(RuntimeError):
    pass


class AirtableClient:
    """Minimal Airtable REST client. Bearer-token auth, ID-addressed.

    Endpoint shapes mirror the bundled MCP server's airtableService.ts:
      list bases      GET  /v0/meta/bases
      describe tables GET  /v0/meta/bases/{baseId}/tables
      list records    GET  /v0/{baseId}/{tableId}
      create record   POST /v0/{baseId}/{tableId}
      update records   PATCH /v0/{baseId}/{tableId}
      delete records  DELETE /v0/{baseId}/{tableId}?records[]=...
    """

    def __init__(self, api_key: str, base_url: str = AIRTABLE_BASE_URL) -> None:
        api_key = (api_key or '').strip()
        if not api_key:
            raise AirtableError(
                'AIRTABLE_API_KEY is not set. Export an Airtable Personal Access Token before running the harness.'
            )
        self._client = httpx.Client(
            base_url=base_url,
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = self._client.request(method, path, **kw)
        except httpx.HTTPError as e:  # network / DNS / timeout
            raise AirtableError(f'{method} {path} — transport error: {e}') from e
        if resp.status_code >= 400:
            body = resp.text[:400]
            raise AirtableError(f'{method} {path} — HTTP {resp.status_code}: {body}')
        if not resp.content:
            return {}
        return resp.json()

    def list_bases(self) -> list[dict[str, Any]]:
        data = self._request('GET', '/v0/meta/bases')
        return data.get('bases', [])

    def list_tables(self, base_id: str) -> list[dict[str, Any]]:
        data = self._request('GET', f'/v0/meta/bases/{base_id}/tables')
        return data.get('tables', [])

    def list_records(
        self,
        base_id: str,
        table_id: str,
        *,
        max_records: int = 1,
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[tuple[str, str]] = [('maxRecords', str(max_records))]
        for f in fields or []:
            params.append(('fields[]', f))
        data = self._request('GET', f'/v0/{base_id}/{table_id}', params=params)
        return data.get('records', [])

    def create_record(self, base_id: str, table_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            'POST',
            f'/v0/{base_id}/{table_id}',
            json={'fields': fields, 'typecast': True},
        )

    def get_record(self, base_id: str, table_id: str, record_id: str) -> dict[str, Any]:
        return self._request('GET', f'/v0/{base_id}/{table_id}/{record_id}')

    def update_record(self, base_id: str, table_id: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        resp = self._request(
            'PATCH',
            f'/v0/{base_id}/{table_id}',
            json={
                'records': [{'id': record_id, 'fields': fields}],
                'typecast': True,
            },
        )
        return resp['records'][0]

    def delete_record(self, base_id: str, table_id: str, record_id: str) -> None:
        self._request(
            'DELETE',
            f'/v0/{base_id}/{table_id}',
            params=[('records[]', record_id)],
        )


# --------------------------------------------------------------------------- #
# Contract loading
# --------------------------------------------------------------------------- #


def load_contract(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AirtableError(f'Golden contract not found: {path}')
    return json.loads(path.read_text())


# --------------------------------------------------------------------------- #
# Runtime name->id resolution (inline mirror of pipeline/config.AirtableResolver)
# --------------------------------------------------------------------------- #

#: Optional per-table NAME overrides, keyed by the contract's live table name.
#: Mirrors the pipeline's AIRTABLE_*_TABLE env vars so a base whose tables are
#: named differently can be pointed at without editing the contract. When unset,
#: the contract's own (live) name is used as-is.
TABLE_NAME_ENV_OVERRIDES: dict[str, str] = {
    'Inventory Cards': 'AIRTABLE_CARDS_TABLE',
    'Decks': 'AIRTABLE_DECKS_TABLE',
    'Trades': 'AIRTABLE_TRADES_TABLE',
    'Chase Cards': 'AIRTABLE_CHASE_TABLE',
}


def live_table_name(contract_name: str) -> str:
    """Map a contract table name to the live name for this instance (env override)."""
    env_var = TABLE_NAME_ENV_OVERRIDES.get(contract_name)
    if env_var:
        override = os.environ.get(env_var)
        if override:
            return override
    return contract_name


class SchemaResolver:
    """Resolve table NAMES to per-base ``tbl…`` ids at runtime (GET-only, cached).

    Inline mirror of ``pipeline/config.py``'s ``AirtableResolver``: this standalone
    PEP-723 script cannot import the pipeline package, so it re-implements the small
    resolve-by-name here. Fetches the base schema ONCE via the meta API and answers
    every subsequent name->id / field-name query from the cached payload.
    """

    def __init__(self, client: AirtableClient, base_id: str) -> None:
        self._client = client
        self._base_id = base_id
        self._by_name: dict[str, dict[str, Any]] | None = None

    def _ensure_loaded(self) -> None:
        if self._by_name is not None:
            return
        by_name: dict[str, dict[str, Any]] = {}
        for t in self._client.list_tables(self._base_id):
            name = t.get('name')
            tid = t.get('id')
            if not name or not tid:
                continue
            by_name[name] = {
                'id': tid,
                'name': name,
                'field_names': {f['name'] for f in t.get('fields', []) if f.get('name')},
            }
        self._by_name = by_name

    def resolve(self, contract_name: str) -> dict[str, Any] | None:
        """Return the ``{id, name, field_names}`` entry for a contract table name.

        Applies the AIRTABLE_*_TABLE override, then looks the live name up in the
        cached base schema. Returns ``None`` when the table is absent (the caller
        reports it as a FAILing contract assertion).
        """
        self._ensure_loaded()
        assert self._by_name is not None
        return self._by_name.get(live_table_name(contract_name))


# --------------------------------------------------------------------------- #
# Read checks: golden-contract assertions
# --------------------------------------------------------------------------- #


def run_read_checks(
    client: AirtableClient,
    base_id: str,
    contract: dict[str, Any],
    resolver: SchemaResolver,
) -> list[FlowReport]:
    """Assert every table is reachable and every contract field still exists.

    Tables are addressed by NAME: the ``tbl…`` id is resolved at runtime from the
    live base schema (via ``resolver``), never read from the contract.
    """
    reports: list[FlowReport] = []

    for skill, spec in contract['skills'].items():
        report = FlowReport(skill=skill)
        for table_name, tspec in spec['tables'].items():
            entry = resolver.resolve(table_name)
            if entry is None:
                report.add(
                    f'table:{table_name}',
                    Status.FAIL,
                    f'table {table_name!r} (live name {live_table_name(table_name)!r}) '
                    f'not found in base {base_id}',
                )
                continue
            table_id = entry['id']

            # Reachability probe: a live list_records call (maxRecords=1).
            try:
                client.list_records(base_id, table_id, max_records=1)
                reachable = True
            except AirtableError as e:
                reachable = False
                report.add(
                    f'reach:{table_name}',
                    Status.FAIL,
                    f'list_records failed: {e}',
                )

            present = entry['field_names']
            missing = [f for f in tspec['required_fields'] if f not in present]
            if missing:
                report.add(
                    f'fields:{table_name}',
                    Status.FAIL,
                    f'missing required field(s): {", ".join(missing)}',
                )
            elif reachable:
                report.add(
                    f'fields:{table_name}',
                    Status.PASS,
                    f'{len(tspec["required_fields"])} field(s) present, table reachable',
                )
        reports.append(report)
    return reports


# --------------------------------------------------------------------------- #
# Write round-trip: self-cleaning scratch record
# --------------------------------------------------------------------------- #


def run_write_roundtrip(
    client: AirtableClient,
    base_id: str,
    contract: dict[str, Any],
    resolver: SchemaResolver,
) -> FlowReport:
    """create -> read-back -> update -> verify -> DELETE. Cleans up on failure.

    The scratch table is addressed by NAME; its ``tbl…`` id is resolved at runtime.
    """
    report = FlowReport(skill='write-roundtrip')
    sw = contract['scratch_write']
    scratch_table = sw['table']
    entry = resolver.resolve(scratch_table)
    if entry is None:
        report.add(
            'roundtrip',
            Status.FAIL,
            f'scratch table {scratch_table!r} (live name {live_table_name(scratch_table)!r}) '
            f'not found in base {base_id}',
        )
        return report
    table_id = entry['id']
    name_field = sw['name_field']
    update_field = sw['update_field']
    scratch_name = f'{sw["name_prefix"]}{int(time.time())}'

    record_id: str | None = None
    try:
        # create
        created = client.create_record(base_id, table_id, {name_field: scratch_name})
        record_id = created.get('id')
        if not record_id:
            report.add('create', Status.FAIL, 'create returned no record id')
            return report
        report.add('create', Status.PASS, f'created scratch record {record_id}')

        # read-back
        fetched = client.get_record(base_id, table_id, record_id)
        if fetched.get('fields', {}).get(name_field) == scratch_name:
            report.add('read-back', Status.PASS, 'name matches on read-back')
        else:
            report.add(
                'read-back',
                Status.FAIL,
                f'expected {name_field}={scratch_name!r}, got {fetched.get("fields", {}).get(name_field)!r}',
            )

        # update
        marker = f'regression-marker-{int(time.time())}'
        client.update_record(base_id, table_id, record_id, {update_field: marker})
        verify = client.get_record(base_id, table_id, record_id)
        if verify.get('fields', {}).get(update_field) == marker:
            report.add('update', Status.PASS, f'{update_field} updated + verified')
        else:
            report.add(
                'update',
                Status.FAIL,
                f'expected {update_field}={marker!r}, got {verify.get("fields", {}).get(update_field)!r}',
            )
    except AirtableError as e:
        report.add('roundtrip', Status.FAIL, str(e))
    finally:
        if record_id is not None:
            try:
                client.delete_record(base_id, table_id, record_id)
                report.add('delete', Status.PASS, f'deleted scratch {record_id}')
            except AirtableError as e:
                report.add(
                    'delete',
                    Status.FAIL,
                    f'CLEANUP FAILED — scratch record {record_id} may remain: {e}',
                )
    return report


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def print_report(reports: list[FlowReport], *, base_id: str, mode: str) -> bool:
    print(f'\n=== Airtable Regression Harness — mode={mode} base={base_id} ===\n')
    all_pass = True
    for r in reports:
        icon = {'PASS': '[PASS]', 'FAIL': '[FAIL]', 'SKIP': '[SKIP]'}[r.status.value]
        print(f'{icon} {r.skill}')
        for c in r.checks:
            sub = {'PASS': '  ok  ', 'FAIL': '  XX  ', 'SKIP': '  --  '}[c.status.value]
            print(f'    {sub} {c.name}: {c.detail}')
        if r.status is Status.FAIL:
            all_pass = False
        print()
    verdict = 'PASS' if all_pass else 'FAIL'
    print(f'=== OVERALL: {verdict} ===\n')
    return all_pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _resolve_base_id(contract: dict[str, Any], base_id_opt: str | None) -> str:
    return base_id_opt or os.environ.get('AIRTABLE_BASE_ID') or contract['base']['id']


@app.command()
def check(
    read_only: Annotated[
        bool,
        typer.Option('--read-only', help='Reads + contract assertions only (safe).'),
    ] = False,
    full: Annotated[
        bool,
        typer.Option('--full', help='Adds the self-cleaning scratch write round-trip.'),
    ] = False,
    smoke_any_base: Annotated[
        bool,
        typer.Option(
            '--smoke-any-base',
            help=(
                "Mechanics smoke: ignore the contract's base and just prove "
                'HTTP/auth/reporting plumbing against the first base + table the '
                'token can see. For use with a token that cannot see the real base.'
            ),
        ),
    ] = False,
    base_id: Annotated[
        str | None,
        typer.Option('--base-id', help='Override base ID (else env/contract default).'),
    ] = None,
    contract_path: Annotated[
        Path,
        typer.Option('--contract', help='Path to golden_contract.json.'),
    ] = DEFAULT_CONTRACT_PATH,
) -> None:
    """Run the regression check. Default (no flag) behaves as --read-only."""
    if not read_only and not full:
        read_only = True
    if full and read_only:
        # --full is a superset of read-only; treat --full as authoritative.
        read_only = False

    try:
        contract = load_contract(contract_path)
    except AirtableError as e:
        typer.echo(f'ERROR: {e}', err=True)
        raise typer.Exit(1) from e

    api_key = os.environ.get('AIRTABLE_API_KEY', '')
    try:
        client = AirtableClient(api_key)
    except AirtableError as e:
        typer.echo(f'ERROR: {e}', err=True)
        raise typer.Exit(1) from e

    with client:
        if smoke_any_base:
            ok = _run_smoke(client)
            raise typer.Exit(0 if ok else 1)

        resolved_base = _resolve_base_id(contract, base_id)
        mode = 'full' if full else 'read-only'
        # One meta call resolves every contract table NAME -> live tbl… id, cached
        # and reused across read checks + the write round-trip.
        resolver = SchemaResolver(client, resolved_base)

        try:
            reports = run_read_checks(client, resolved_base, contract, resolver)
        except AirtableError as e:
            typer.echo(f'ERROR (read checks): {e}', err=True)
            raise typer.Exit(1) from e

        if full:
            reports.append(run_write_roundtrip(client, resolved_base, contract, resolver))
        else:
            skip = FlowReport(skill='write-roundtrip')
            skip.add('roundtrip', Status.SKIP, 'skipped in --read-only mode')
            reports.append(skip)

        ok = print_report(reports, base_id=resolved_base, mode=mode)
        raise typer.Exit(0 if ok else 1)


def _run_smoke(client: AirtableClient) -> bool:
    """Prove HTTP/auth/reporting plumbing against whatever the token can see.

    Does NOT use the golden contract or the real base. Lists bases, picks the
    first, describes its tables, and does a single list_records read.
    """
    report = FlowReport(skill='mechanics-smoke')
    try:
        bases = client.list_bases()
    except AirtableError as e:
        report.add('list_bases', Status.FAIL, str(e))
        print_report([report], base_id='(discovered)', mode='smoke')
        return False

    if not bases:
        report.add('list_bases', Status.FAIL, 'token can see zero bases')
        print_report([report], base_id='(none)', mode='smoke')
        return False

    base = bases[0]
    base_id = base['id']
    report.add(
        'list_bases',
        Status.PASS,
        f'{len(bases)} base(s) visible; using {base.get("name")!r} ({base_id})',
    )

    try:
        tables = client.list_tables(base_id)
    except AirtableError as e:
        report.add('list_tables', Status.FAIL, str(e))
        print_report([report], base_id=base_id, mode='smoke')
        return False

    if not tables:
        report.add('list_tables', Status.FAIL, 'base has zero tables')
        print_report([report], base_id=base_id, mode='smoke')
        return False

    t = tables[0]
    report.add(
        'describe_table',
        Status.PASS,
        f'{len(tables)} table(s); first {t.get("name")!r} has {len(t.get("fields", []))} field(s)',
    )

    try:
        recs = client.list_records(base_id, t['id'], max_records=1)
        report.add(
            'list_records',
            Status.PASS,
            f'read {len(recs)} record(s) from {t.get("name")!r}',
        )
    except AirtableError as e:
        report.add('list_records', Status.FAIL, str(e))
        print_report([report], base_id=base_id, mode='smoke')
        return False

    return print_report([report], base_id=base_id, mode='smoke')


@app.command('list-bases')
def list_bases_cmd() -> None:
    """Print every base the token can see (plumbing / discovery helper)."""
    api_key = os.environ.get('AIRTABLE_API_KEY', '')
    try:
        with AirtableClient(api_key) as client:
            bases = client.list_bases()
    except AirtableError as e:
        typer.echo(f'ERROR: {e}', err=True)
        raise typer.Exit(1) from e
    if not bases:
        typer.echo('(token can see zero bases)')
        return
    for b in bases:
        typer.echo(f'{b["id"]}  {b.get("name", "?")}  perm={b.get("permissionLevel", "?")}')


if __name__ == '__main__':
    app()
