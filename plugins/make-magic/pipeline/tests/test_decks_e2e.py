"""Phase 5 — BEHAVIORAL end-to-end proof of both guided-build loops.

Unlike the unit tests (store/sync/ledger) and the thin-plumbing CLI tests, these
drive the FULL guided build the way the ``building-decks`` orchestrator does —
chained real CLI verbs through ``cli.main`` — and assert the COMMITTED deck on the
source of record is correct. Two loops:

- CLEAN-SLATE: ``new-draft`` (ephemeral) -> grow + author strategy/focus ->
  VALIDATE (``deck-combos``) -> ``promote-deck`` -> the source deck is synced +
  correct.
- IMPROVE-EXISTING: an ``--from`` exploration copy leaves the original UNTOUCHED
  until ``promote-deck`` lands the change; ``archive-deck`` then hides the draft
  from the default listing.

OFFLINE: tmp ``MAKE_MAGIC_DATA_DIR`` + local YAML backend + a stub resolver (no
Scryfall, no network). The combo lake is absent, so ``deck-combos`` exercises the
HONEST-DEGRADATION path (``combo_data_available: false``) — an inconclusive, not a
clean, bill.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import store
from pipeline.collection import run as cli
from pipeline.contracts import Card, DeckCard


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


def _get_deck(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, name: str) -> dict:
    capsys.readouterr()  # drop anything buffered
    _run(monkeypatch, 'get-deck', name)
    return json.loads(capsys.readouterr().out)


def _save_source_deck(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, cards: list[dict[str, object]], *, format_: str
) -> None:
    payload: dict[str, object] = {'name': name, 'cards': cards, 'format': format_}
    deck_json = tmp_path / f'{name}.json'
    deck_json.write_text(json.dumps(payload))
    _run(monkeypatch, 'save-deck', '--from-json', str(deck_json))


def _commander_cards(commander: str = 'Grumgully, the Generous', *, filler: int = 99) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = [{'name': commander, 'role': 'commander'}]
    cards.extend({'name': f'Filler {i}'} for i in range(filler))
    return cards


# --------------------------------------------------------------------------- #
# LOOP 1 — clean-slate: draft -> grow -> author -> VALIDATE -> commit
# --------------------------------------------------------------------------- #


def test_clean_slate_build_commits_a_correct_synced_deck(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    # FRAME: a clean-slate ephemeral draft, commander only.
    _run(monkeypatch, 'new-draft', 'Fresh', '--commander', 'Grumgully, the Generous', '--format', 'Commander')
    out = capsys.readouterr().out
    assert '[ephemeral]' in out

    # It is ephemeral, and the source of record does NOT exist yet.
    _run(monkeypatch, 'list-decks')
    listing = capsys.readouterr().out
    assert 'Fresh' in listing and '[ephemeral]' in listing

    # FRAME/author: strategy + curated identity land on the draft (local only).
    _run(monkeypatch, 'set-strategy', 'Fresh', 'Go wide with +1/+1 counters, then overrun.')
    capsys.readouterr()
    _run(monkeypatch, 'set-focus-otags', 'Fresh', 'counters-matter', 'go-wide')
    capsys.readouterr()

    # Grow the skeleton to a legal 100 (commander + 99).
    for i in range(99):
        _run(monkeypatch, 'deck-add', 'Fresh', f'Filler {i}')
        capsys.readouterr()

    # ASSESS on the DRAFT (B4 in the loop): factsheet must run on an EPHEMERAL draft
    # (no source of record) — it used to FileNotFound. Routes through the local store.
    capsys.readouterr()
    _run(monkeypatch, 'factsheet', 'Fresh')
    factsheet = json.loads(capsys.readouterr().out)
    assert factsheet['deck'] == 'Fresh'  # a real factsheet keyed to the draft

    # PROVENANCE substrate: set-assessment stamps freshness against the current
    # version; list-decks --json reads it back as `assessment: fresh` (M7 stored,
    # not remembered) — and it holds on an ephemeral draft, cross-session.
    _run(monkeypatch, 'set-assessment', 'Fresh', 'Wide counters plan looks coherent.')
    capsys.readouterr()
    _run(monkeypatch, 'list-decks', '--json')
    draft_row = next(r for r in json.loads(capsys.readouterr().out) if r['name'] == 'Fresh')
    assert draft_row['assessment'] == 'fresh'

    # A subsequent content edit moves the version -> the assessment stamp goes STALE
    # (derived phase, not a remembered flag).
    _run(monkeypatch, 'deck-add', 'Fresh', 'Sol Ring')
    capsys.readouterr()
    _run(monkeypatch, 'list-decks', '--json')
    draft_row = next(r for r in json.loads(capsys.readouterr().out) if r['name'] == 'Fresh')
    assert draft_row['assessment'] == 'stale'
    # Undo that extra add so the deck returns to its legal 100 for the promote below.
    _run(monkeypatch, 'deck-remove', 'Fresh', 'Sol Ring')
    capsys.readouterr()

    # VALIDATE: the combo fold runs and DEGRADES HONESTLY (no lake here).
    capsys.readouterr()
    _run(monkeypatch, 'deck-combos', 'Fresh')
    combo = json.loads(capsys.readouterr().out)
    assert combo['combo_data_available'] is False  # inconclusive, not "clean"
    assert combo['combos'] == []  # never implies "no combos found"

    # The draft is still ephemeral — nothing committed to the source yet.
    _run(monkeypatch, 'list-decks')
    assert '[synced]' not in capsys.readouterr().out

    # COMMIT: promote through the ceremony (create-through-ceremony).
    _run(monkeypatch, 'promote-deck', 'Fresh', '--to', 'Fresh')
    assert '[synced]' in capsys.readouterr().out

    # The committed deck on the SOURCE OF RECORD is correct.
    deck = _get_deck(monkeypatch, capsys, 'Fresh')
    assert deck['strategy'] == 'Go wide with +1/+1 counters, then overrun.'
    assert set(deck['focus_otags']) == {'counters-matter', 'go-wide'}
    assert sum(c['quantity'] for c in deck['cards']) == 100
    assert any(c['name'] == 'Grumgully, the Generous' and c.get('role') == 'commander' for c in deck['cards'])

    # And it now lists as synced.
    _run(monkeypatch, 'list-decks')
    listing = capsys.readouterr().out
    assert 'Fresh' in listing and '[synced]' in listing


# --------------------------------------------------------------------------- #
# LOOP 2 — improve-existing: explore copy, original untouched, promote, archive
# --------------------------------------------------------------------------- #


def test_improve_existing_explore_copy_leaves_original_until_promote(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    _save_source_deck(monkeypatch, tmp_path, 'Gruul', _commander_cards(), format_='Commander')
    capsys.readouterr()

    # Explore: an ephemeral COPY. Edits land on the copy, never the original.
    _run(monkeypatch, 'new-draft', 'Gruul (explore)', '--from', 'Gruul')
    assert '[ephemeral]' in capsys.readouterr().out

    _run(monkeypatch, 'deck-swap', 'Gruul (explore)', '--add', 'Lightning Bolt', '--cut', 'Filler 0', '--why', 'test')
    capsys.readouterr()

    # The ORIGINAL is untouched — Filler 0 still present, no Bolt.
    original = _get_deck(monkeypatch, capsys, 'Gruul')
    orig_names = {c['name'] for c in original['cards']}
    assert 'Filler 0' in orig_names
    assert 'Lightning Bolt' not in orig_names

    # The COPY has the swap.
    copy = _get_deck(monkeypatch, capsys, 'Gruul (explore)')
    copy_names = {c['name'] for c in copy['cards']}
    assert 'Lightning Bolt' in copy_names
    assert 'Filler 0' not in copy_names

    # COMMIT the exploration back onto the original, then hide the draft.
    _run(monkeypatch, 'promote-deck', 'Gruul (explore)', '--to', 'Gruul')
    capsys.readouterr()

    promoted = _get_deck(monkeypatch, capsys, 'Gruul')
    promoted_names = {c['name'] for c in promoted['cards']}
    assert 'Lightning Bolt' in promoted_names  # the change landed
    assert 'Filler 0' not in promoted_names
    assert sum(c['quantity'] for c in promoted['cards']) == 100  # size preserved

    # P3: promote AUTO-CONSUMES the exploration draft — it is already consumed +
    # archived (a retired lineage), so no explicit archive step is needed and it is
    # decluttered out of the default list. The original 'Gruul' remains, now carrying
    # the change (the single synced row for that source — B1/B2 killed).
    from pipeline.decks import DecksStore
    from pipeline.decks.store import DecksError

    decks = DecksStore()
    rows = {r.name: r for r in decks.list_rows(include_archived=True)}
    assert rows['Gruul (explore)'].sync_status == 'consumed'
    assert rows['Gruul (explore)'].archived is True

    # CONSUME LIFECYCLE (explicit): the consumed draft is INERT. A deck-add on it is
    # REFUSED and creates NO new source file — the content lives on the parent now.
    explore_uuid = rows['Gruul (explore)'].deck_uuid
    with pytest.raises(DecksError):
        decks.add_card(explore_uuid, DeckCard(name='Mountain', quantity=1))
    # The draft never materialized a source deck of its own (B2 zombie killed).
    with pytest.raises(FileNotFoundError):
        cli._store().get_deck('Gruul (explore)')

    _run(monkeypatch, 'list-decks')
    default_listing = capsys.readouterr().out
    assert 'Gruul (explore)' not in default_listing  # decluttered (consumed + archived)
    assert 'Gruul [synced]' in default_listing  # the real deck stays, with the change
