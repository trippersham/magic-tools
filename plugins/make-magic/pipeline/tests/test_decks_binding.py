"""The ``read_bound_source`` chokepoint — every source read goes by the row's
bound external ref (in-file uuid for ``local``, recordId for ``airtable``), NEVER
by name.

A source-of-record read that goes by NAME instead of by the bound ref serves the
wrong deck. These pin the chokepoint:

- ``--id`` / explicit ``pull`` / TTL-expiry reads serve the BOUND deck, not the
  base-slug deck;
- promote onto a RENAMED parent file still fires the drift guard (bound read),
  preserving a concurrent foreign edit;
- ``read_bound_source`` returns None on a dead bound ref (never a name-read of a
  same-named decoy), and ``expected_ref`` reads strictly that ref.

Everything OFFLINE: a real ``LocalYamlStore`` on a tmp dir is the source of record
and a REAL canonicalizing resolver hydrates cards (never stubbed to identity).
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from _decks_helpers import (
    commander_deck,
    decks_dir,
    file_uuid,
    require_file_uuid,
    save_source,
    source_store,
    write_legacy_yaml,
)

from pipeline.contracts import DeckCard
from pipeline.decks import DecksStore
from pipeline.decks.sync import SyncDriftError, promote, read_bound_source
from pipeline.decks.version import version


def _synced_rows(data_dir: Path) -> list[tuple[str, str, str | None]]:
    d = DecksStore()
    return [(r.deck_uuid, r.name, r.external_ids) for r in d.list_rows() if r.sync_status == 'synced']


# --------------------------------------------------------------------------- #
# The --id / pull / TTL read serves the BOUND deck, not the base slug.
# --------------------------------------------------------------------------- #


def test_id_read_serves_the_bound_deck_not_the_base_slug(cli, data_dir: Path) -> None:
    """After a dup-name clean-slate promote, a ``--id`` read of the promoted row
    must serve ITS OWN (2-card) content, never the unrelated 100-card base slug."""
    save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
    cli('get-deck', 'Precious')  # pull -> row A bound to precious.yaml
    cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')
    cli('deck-add', 'JunkB', 'Junk Card 1')
    code, _out, err = cli('promote-deck', 'JunkB', '--to', 'Precious')
    assert code == 0, err

    base_uuid = require_file_uuid(decks_dir(data_dir) / 'precious.yaml')
    promoted = None
    for deck_uuid, name, ext in _synced_rows(data_dir):
        if name != 'Precious':
            continue
        bound = json.loads(ext or '{}').get('local')
        if bound != base_uuid:
            promoted = deck_uuid
    assert promoted is not None, 'expected a promoted Precious row bound to its own file'

    # --id read (freshness unset after promote -> would pull) must serve 2 cards.
    code, out, _err = cli('get-deck', '--id', promoted[:8])
    assert code == 0
    served = json.loads(out)
    assert sum(c.get('quantity', 1) for c in served['cards']) == 2

    # An explicit pull of the promoted row also stays on its own file.
    cli('pull', '--id', promoted[:8])
    code, out, _err = cli('get-deck', '--id', promoted[:8])
    assert sum(c.get('quantity', 1) for c in json.loads(out)['cards']) == 2

    # The base-slug row + file are untouched (still 100 cards).
    orig = source_store(data_dir).get_deck_by_uuid(base_uuid)
    assert sum(c.quantity for c in orig.cards) == 100


def test_ttl_expiry_read_stays_on_the_bound_file(cli, data_dir: Path) -> None:
    """A TTL-expiry re-pull (freshness cleared) reads the row's BOUND file, not the slug."""
    save_source(cli, data_dir, 'Precious', filler=99, prefix='OrigCard')
    cli('get-deck', 'Precious')
    cli('new-draft', 'JunkB', '--commander', 'Grumgully, the Generous', '--format', 'Commander')
    cli('deck-add', 'JunkB', 'Junk Card 1')
    cli('promote-deck', 'JunkB', '--to', 'Precious')

    base_uuid = file_uuid(decks_dir(data_dir) / 'precious.yaml')
    decks = DecksStore()
    promoted = next(
        u for u, n, e in _synced_rows(data_dir) if n == 'Precious' and json.loads(e or '{}').get('local') != base_uuid
    )
    # Force TTL expiry: clear freshness so the very next read re-pulls.
    decks.set_freshness(promoted, {})
    code, out, _err = cli('get-deck', '--id', promoted[:8])
    assert code == 0
    assert sum(c.get('quantity', 1) for c in json.loads(out)['cards']) == 2


# --------------------------------------------------------------------------- #
# Promote onto a RENAMED parent file fires the drift guard (bound read).
# --------------------------------------------------------------------------- #


def test_promote_on_renamed_parent_fires_drift_guard(cli, data_dir: Path) -> None:
    """Renaming the parent FILE is supported; a foreign edit to it must NOT be
    silently destroyed by an exploration promote — the bound read sees the drift."""
    save_source(cli, data_dir, 'Gruul', filler=99, prefix='GCard')
    cli('get-deck', 'Gruul')
    cli('new-draft', 'Explore', '--from', 'Gruul')
    cli('deck-swap', 'Explore', '--cut', 'GCard 0', '--add', 'Improvement Card')

    # The user renames the deck FILE (explicitly supported); a foreign writer edits it.
    d = decks_dir(data_dir)
    (d / 'gruul.yaml').rename(d / 'krenko-tribal.yaml')
    p = d / 'krenko-tribal.yaml'
    p.write_text(p.read_text() + '- card: Foreign Addition\n')

    # Promote must refuse (drift) — the bound read finds the renamed file's foreign edit.
    code, _out, _err = cli('promote-deck', 'Explore')
    assert code != 0, 'promote onto a drifted (renamed) parent must refuse, not clobber'
    assert 'Foreign Addition' in p.read_text(), 'the concurrent foreign edit must survive'
    assert 'Improvement Card' not in p.read_text()


def test_promote_bound_read_preserves_foreign_edit(data_dir: Path) -> None:
    """Unit: a renamed parent file with a foreign edit -> promote raises SyncDriftError."""
    src = source_store(data_dir)
    parent = commander_deck('Gruul', filler=99, prefix='GCard')
    src.save_deck(parent)
    file_uuid_ = require_file_uuid(decks_dir(data_dir) / 'gruul.yaml')

    decks = DecksStore()
    p_uuid = uuid4().hex
    stored = src.get_deck_by_uuid(file_uuid_).model_copy(update={'uuid': p_uuid})
    decks.put(
        stored,
        deck_uuid=p_uuid,
        sync_status='synced',
        source_ref='Gruul',
        synced_baseline=version(stored),
        rationale='pull',
    )
    decks.set_external_id(p_uuid, 'local', file_uuid_)

    draft = stored.model_copy(update={'name': 'Explore', 'uuid': uuid4().hex})
    d_uuid = decks.create_ephemeral(draft, derived_from=p_uuid)
    decks.swap(d_uuid, add=DeckCard(name='Improvement'), cut='GCard 0')

    # Rename the file + a foreign writer appends a card (drift).
    d = decks_dir(data_dir)
    (d / 'gruul.yaml').rename(d / 'renamed.yaml')
    ren = d / 'renamed.yaml'
    ren.write_text(ren.read_text() + '- card: Foreign Addition\n')

    with pytest.raises(SyncDriftError):
        promote(decks, src, deck_uuid=d_uuid)
    assert 'Foreign Addition' in ren.read_text()


# --------------------------------------------------------------------------- #
# The chokepoint itself — direct unit coverage of read_bound_source.
# --------------------------------------------------------------------------- #


def test_read_bound_source_returns_none_when_bound_ref_is_gone(data_dir: Path) -> None:
    """A bound ref that resolves to nothing (deleted file) returns None — never a
    name-read of a different object, never a crash."""
    src = source_store(data_dir)
    src.save_deck(commander_deck('Ghost', filler=99))
    file_uuid_ = require_file_uuid(decks_dir(data_dir) / 'ghost.yaml')
    decks = DecksStore()
    g_uuid = uuid4().hex
    stored = src.get_deck_by_uuid(file_uuid_).model_copy(update={'uuid': g_uuid})
    decks.put(
        stored,
        deck_uuid=g_uuid,
        sync_status='synced',
        source_ref='Ghost',
        synced_baseline=version(stored),
        rationale='pull',
    )
    decks.set_external_id(g_uuid, 'local', file_uuid_)

    # Delete the file the row is bound to; another same-named file appears.
    (decks_dir(data_dir) / 'ghost.yaml').unlink()
    write_legacy_yaml(data_dir, 'ghost-decoy', 'Ghost', [f'Decoy {i}' for i in range(5)])

    got = read_bound_source(decks, src, deck_uuid=g_uuid, source_ref='Ghost')
    assert got is None, 'a dead bound ref returns None, never the same-named decoy'


def test_read_bound_source_expected_ref_reads_that_ref(data_dir: Path) -> None:
    """``expected_ref`` reads strictly by that ref (reread-after-create), never by name."""
    src = source_store(data_dir)
    src.save_deck(commander_deck('Base', filler=99))
    base_uuid = require_file_uuid(decks_dir(data_dir) / 'base.yaml')
    # A second same-named file with its own uuid.
    write_legacy_yaml(data_dir, 'base-two', 'Base', [f'Two {i}' for i in range(5)])
    src.backfill_deck_uuids()
    two_uuid = require_file_uuid(decks_dir(data_dir) / 'base-two.yaml')

    decks = DecksStore()
    b_uuid = uuid4().hex
    decks.put(
        commander_deck('Base').model_copy(update={'uuid': b_uuid}),
        deck_uuid=b_uuid,
        sync_status='synced',
        source_ref='Base',
        synced_baseline='x',
        rationale='pull',
    )
    decks.set_external_id(b_uuid, 'local', base_uuid)

    got = read_bound_source(decks, src, deck_uuid=b_uuid, source_ref='Base', expected_ref=two_uuid)
    assert got is not None
    assert any(c.name.startswith('Two') for c in got.cards), 'expected_ref must win over the bound ref + name'
