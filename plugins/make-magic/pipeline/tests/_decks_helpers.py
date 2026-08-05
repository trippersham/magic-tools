"""Shared offline harness for the decks behavior suite.

One home for the pieces the decks hardening tests all lean on:

- a REAL canonicalizing resolver (``CanonicalizingResolver``) — the load-bearing
  hazard is exercised here, NEVER stubbed to identity;
- an in-process CLI runner (``run_cli``) that drives the real ``collection`` verbs
  and returns ``(exit_code, stdout, stderr)``;
- the current-adapter Airtable **contract double** (``MockAirtableDecks``) — it
  mirrors the adapter's own create/update targeting rule verbatim (create stamps
  the recordId back in place; update of a deleted record raises 422) with ZERO
  network and ZERO prod writes;
- the deck-authoring / filesystem helpers the CLI tests share.

The matching ``data_dir`` / ``cli`` fixtures live in ``conftest.py``.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from pipeline.collection.adapters.local_yaml import LocalYamlStore
from pipeline.contracts import Card, Deck, DeckCard
from pipeline.decks import DecksStore


class CanonicalizingResolver:
    """A REAL canonicalizing resolver (never stubbed to identity — the hazard)."""

    _CANON: ClassVar[dict[str, str]] = {
        'krenko, mob boss': 'Krenko, Mob Boss',
        'grumgully, the generous': 'Grumgully, the Generous',
        'zada, hedron grinder': 'Zada, Hedron Grinder',
        'mountain': 'Mountain',
        'sol ring': 'Sol Ring',
        'impact tremors': 'Impact Tremors',
    }

    def _canonical(self, name: str) -> str:
        key = ' '.join(name.split()).casefold()
        return self._CANON.get(key, ' '.join(name.split()))

    def get_card(self, name: str) -> Card | None:
        canonical = self._canonical(name)
        return Card(name=canonical, oracle_id=f'oid-{canonical}', mana_cost='{1}{R}')


# --------------------------------------------------------------------------- #
# In-process CLI runner — real verbs.
# --------------------------------------------------------------------------- #


def run_cli(*argv: str) -> tuple[int, str, str]:
    """Run one collection CLI verb in-process; return (exit_code, stdout, stderr)."""
    from pipeline.collection import run as cli

    old_argv = sys.argv
    sys.argv = ['collection', *argv]
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = int(exc.code or 0)
            except Exception as exc:  # a raw traceback escaping the CLI = a crash.
                code = -1
                import traceback

                err.write(''.join(traceback.format_exception(exc)))
    finally:
        sys.argv = old_argv
    return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- #
# Airtable contract double — the adapter's OWN targeting rule, verbatim.
# --------------------------------------------------------------------------- #


class MockAirtableDecks:
    """Contract double mirroring the CURRENT adapter's create/update targeting rule.

    ``save_deck`` on a deck carrying ``airtable_record_id`` UPDATES that record;
    otherwise it CREATES a fresh one and STAMPS the new recordId back onto the
    passed ``deck`` instance (so a caller rereads THIS record, never a name
    sibling). Update of a DELETED record raises 422; ``fail_create`` simulates a
    503 create outage. ``force_fresh`` is a no-op on Airtable (create/update is
    driven by ``airtable_record_id``); it is accepted for current-adapter parity.
    ZERO network; nothing here writes upstream; no ``delete_record``.
    """

    backend_name = 'airtable'

    def __init__(self) -> None:
        self.records: dict[str, Deck] = {}
        self._n = 0
        self.log: list[str] = []
        self.fail_create = False

    def _mint(self) -> str:
        self._n += 1
        return f'rec{self._n:05d}'

    def get_deck(self, name: str) -> Deck:
        self.log.append(f'get_deck(name={name!r})')
        for rid, d in self.records.items():
            if d.name == name:
                return d.model_copy(update={'airtable_record_id': rid})
        raise FileNotFoundError(f'No Airtable Decks record named {name!r}.')

    def get_deck_by_record_id(self, record_id: str) -> Deck:
        self.log.append(f'get_deck_by_record_id({record_id})')
        if record_id in self.records:
            return self.records[record_id].model_copy(update={'airtable_record_id': record_id})
        raise FileNotFoundError(f'No Airtable Decks record with id {record_id!r}.')

    def save_deck(self, deck: Deck, *, allow_shrink: bool = False, force_fresh: bool = False) -> None:
        if deck.airtable_record_id:
            if deck.airtable_record_id not in self.records:
                self.log.append(f'update_record({deck.airtable_record_id}) -> 422 DELETED')
                raise RuntimeError(f'422: record {deck.airtable_record_id!r} not found')
            self.log.append(f'update_record({deck.airtable_record_id}, name={deck.name!r})')
            self.records[deck.airtable_record_id] = deck.model_copy()
        else:
            if self.fail_create:
                self.log.append('create_record -> 503 (simulated outage)')
                raise RuntimeError('503: Airtable unavailable')
            rid = self._mint()
            self.log.append(f'create_record(-> {rid}, name={deck.name!r})')
            self.records[rid] = deck.model_copy(update={'airtable_record_id': rid})
            deck.airtable_record_id = rid

    def list_decks(self) -> list[Deck]:
        return list(self.records.values())


# --------------------------------------------------------------------------- #
# Filesystem / deck-authoring helpers.
# --------------------------------------------------------------------------- #


def source_store(data_dir: Path) -> LocalYamlStore:
    """A real ``LocalYamlStore`` rooted at ``data_dir``'s collection."""
    return LocalYamlStore(resolver=CanonicalizingResolver(), collection_root=data_dir / 'collection')


def decks_dir(data_dir: Path) -> Path:
    return data_dir / 'collection' / 'decks'


def yaml_files(data_dir: Path, glob: str = '*.yaml') -> list[str]:
    d = decks_dir(data_dir)
    return sorted(p.name for p in d.glob(glob)) if d.exists() else []


def commander_deck(name: str, *, filler: int = 99, prefix: str = 'Filler') -> Deck:
    cards = [DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander')]
    cards += [DeckCard(name=f'{prefix} {i}', quantity=1) for i in range(filler)]
    return Deck(name=name, format='commander', cards=cards)


def file_uuid(path: Path) -> str | None:
    for line in path.read_text().splitlines():
        if line.startswith('uuid: '):
            return line.split('uuid: ', 1)[1].strip() or None
    return None


def require_file_uuid(path: Path) -> str:
    """The file's in-file uuid — asserted present (post-backfill/save)."""
    uuid = file_uuid(path)
    assert uuid is not None, f'{path.name} carries no in-file uuid'
    return uuid


def save_source(cli, data_dir: Path, name: str, *, filler: int = 99, prefix: str = 'Filler') -> None:
    """Author a source deck through ``save-deck --from-json`` (a real first save)."""
    payload = commander_deck(name, filler=filler, prefix=prefix).model_dump(mode='json')
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / 'deck.json'
    p.write_text(json.dumps(payload))
    code, _out, err = cli('save-deck', '--from-json', str(p))
    assert code == 0, err


def save_json(cli, data_dir: Path, payload: dict, *, confirm: bool = False) -> tuple[int, str, str]:
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / 'payload.json'
    p.write_text(json.dumps(payload))
    argv = ['save-deck', '--from-json', str(p)]
    if confirm:
        argv.append('--confirm')
    return cli(*argv)


def expire(name: str | None = None) -> None:
    """Clear the pull TTL so the next read re-consults the source (a bound read)."""
    d = DecksStore()
    for row in d.list_rows():
        if name is None or row.name == name:
            d.set_freshness(row.deck_uuid, {})


def write_legacy_yaml(
    data_dir: Path, slug: str, name: str, cards: Sequence[str], *, uuid: str | None = None
) -> Path:
    d = decks_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if uuid is not None:
        lines.append(f'uuid: {uuid}')
    lines += [f'name: {name}', 'format: Commander', 'cards:', '- card: "Krenko, Mob Boss"', '  role: commander']
    lines += [f'- card: "{c}"' for c in cards]
    p = d / f'{slug}.yaml'
    p.write_text('\n'.join(lines) + '\n')
    return p


def write_source_yaml(
    data_dir: Path, slug: str, name: str, cards: Sequence[tuple[str, str | None]], *, uuid: str | None = None
) -> Path:
    """Hand-author a source YAML the way an older store / a human would (optionally legacy)."""
    d = decks_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if uuid:
        lines.append(f'uuid: {uuid}')
    lines += [f'name: {name}', 'format: Commander', 'cards:']
    for n, role in cards:
        lines.append(f'- card: "{n}"' + (f'\n  role: {role}' if role else ''))
    p = d / f'{slug}.yaml'
    p.write_text('\n'.join(lines) + '\n')
    return p


def legacy_restore(path: Path, name: str, cards: list[str], *, old_mtime: bool = False) -> str:
    """Drop a legacy (no in-file uuid) backup at ``path`` — the restore case.

    ``old_mtime`` back-dates the file so the per-file backfill marker key stays
    unchanged (the ``cp -p`` case): the backfill stays gated, so the row's binding
    stays dead and the restored legacy file is a marker-evading same-named no-uuid
    file at the slug — the exact recovery hazard.
    """
    import os

    body = f'name: {name}\ncards:\n' + ''.join(f'- card: {c}\n' for c in cards)
    path.write_text(body)
    if old_mtime:
        old = path.stat().st_mtime_ns - 10**12
        os.utime(path, ns=(old, old))
    return body
