"""P4 regression tests — the guard layer + undo cursor (Phase 6, §5).

These are the round-4 blocker repros, written FIRST (strict TDD) so each guard
is proven to CLOSE its finding:

- **M5** — ``DeckCard.quantity`` is ``Field(ge=1)`` and the ``deck-add`` /
  ``deck-remove`` argparse rejects ``--qty < 1``, so ``deck-remove --qty -1`` can
  no longer GROW the deck and ``deck-add --qty 0`` can never land a 0-qty entry.
- **m2** — ``remove_card`` refuses cutting the SOLE commander.
- **m1** — ``swap`` / ``add_card`` refuse incrementing an existing commander to
  qty ≥ 2 (a commander is a singleton).
- **M3** — ``deck-add`` / ``deck-swap`` canonicalize the card name via the REAL
  ``default_card_resolver()`` before building the ``DeckCard`` (canonical on a
  lake hit, raw on a miss), so ``lightning bolt`` + ``Lightning Bolt`` collapse to
  ONE entry — still ONE after a push + re-pull.
- **M5-tail** — an edit whose commit-push is REFUSED (shrink guard) auto-undoes
  the local edit so a failed edit never half-lands.
- **M1** — the undo CURSOR walks strictly backward through distinct-content
  versions, continues from the last-restored position across CLI invocations, and
  skips identical-content versions (no oscillation, no deadlock).

The M3 tests use the **REAL canonicalizing resolver** (never a stub) — the load-
bearing rule from R3-B3: a stub would hide the exact hazard the guard exists to
kill. A real ``raw/oracle_cards`` lake with a canonical ``Lightning Bolt`` row is
written so the case-insensitive lake lookup canonicalizes ``lightning bolt``.
Everything else is OFFLINE (tmp ``MAKE_MAGIC_DATA_DIR``, local YAML backend).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pipeline import store
from pipeline.collection import run as cli
from pipeline.collection.resolver import DuckDBCardResolver
from pipeline.contracts import Deck, DeckCard
from pipeline.decks import DecksStore, version

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _offline_404(_request: httpx.Request) -> httpx.Response:
    """A definitive-404 live transport so the REAL resolver never touches the network.

    Every live-fallback lookup 404s -> None (a definitive miss, not transient), so a
    lake MISS degrades to a raw name-only card WITHOUT a network call. Lake HITS
    (seeded canonical rows) still canonicalize — that is the real, un-stubbed
    canonicalization the M3 guard leans on.
    """
    return httpx.Response(404, json={'object': 'error'})


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated tmp data root + local backend + the REAL resolver, OFFLINE.

    ``default_card_resolver`` is repointed to a real ``DuckDBCardResolver`` whose
    live fallback is a MockTransport 404 — so it is the genuine resolver (lake-first
    canonicalization intact), never a stub of the hazard, yet issues zero network.
    """
    root = tmp_path / 'data'
    monkeypatch.setenv(store.ENV_DATA_DIR, str(root))
    monkeypatch.setenv('MAKE_MAGIC_BACKEND', 'local')
    monkeypatch.delenv('AIRTABLE_API_KEY', raising=False)

    def _make_resolver() -> DuckDBCardResolver:
        return DuckDBCardResolver(
            client=httpx.Client(transport=httpx.MockTransport(_offline_404)), min_interval=0.0
        )

    monkeypatch.setattr('pipeline.collection.resolver.default_card_resolver', _make_resolver)
    return root


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr('sys.argv', ['collection', *argv])
    cli.main()


def _commander_cards(commander: str = 'Grumgully, the Generous') -> list[dict[str, object]]:
    """A 100-card Commander list (1 commander + 99 filler) so target guards fire."""
    cards: list[dict[str, object]] = [{'name': commander, 'role': 'commander'}]
    cards.extend({'name': f'Filler {i}'} for i in range(99))
    return cards


def _save_source_deck(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    cards: list[dict[str, object]],
    *,
    format_: str | None = 'Commander',
) -> None:
    payload: dict[str, object] = {'name': name, 'cards': cards}
    if format_ is not None:
        payload['format'] = format_
    deck_json = tmp_path / f'{name}.json'
    deck_json.write_text(json.dumps(payload))
    _run(monkeypatch, 'save-deck', '--from-json', str(deck_json))


def _land_canonical_card(name: str, oracle_id: str) -> None:
    """Write a canonical ``raw/oracle_cards`` row so the REAL resolver canonicalizes.

    The resolver's lake lookup is ``lower(name) = lower(?)``, so a stored canonical
    ``Lightning Bolt`` row resolves an incoming ``lightning bolt`` to ``.name ==
    'Lightning Bolt'`` — the exact canonicalization the M3 guard leans on. No stub.
    """
    row = {
        'oracle_id': oracle_id,
        'name': name,
        'cmc': 1.0,
        'mana_cost': '{R}',
        'type_line': 'Instant',
        'colors': ['R'],
        'color_identity': ['R'],
        'produced_mana': [],
        'keywords': [],
        'oracle_text': 'Lightning Bolt deals 3 damage to any target.',
        'power': None,
        'toughness': None,
        'art_crop': None,
        'scryfall_uri': None,
        'set_name': 'Alpha',
    }
    raw_dir = store.StorePaths.resolve().layer_dir('raw', create=True)
    tmp = raw_dir / '_seed_oracle.json'
    tmp.write_text(json.dumps([row]), encoding='utf-8')
    try:
        with store.connect() as conn:
            rel = conn.read_json(str(tmp))
            store.write_parquet(conn, rel, 'raw', 'oracle_cards')
    finally:
        tmp.unlink(missing_ok=True)


def _get(store_: DecksStore, deck_uuid: str) -> Deck:
    """Read a deck, asserting it exists (narrows ``Deck | None`` -> ``Deck`` for pyright)."""
    deck = store_.get(deck_uuid)
    assert deck is not None
    return deck


def _deck(name: str = 'Krenko', *, strategy: str | None = None) -> Deck:
    cards = [DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander')]
    cards.extend(DeckCard(name=f'Goblin {i}', quantity=1) for i in range(99))
    return Deck(name=name, format='commander', strategy=strategy, cards=cards)


# --------------------------------------------------------------------------- #
# M5 — quantity guard (ge=1 on the model + argparse rejection)
# --------------------------------------------------------------------------- #


def test_deckcard_quantity_zero_is_validation_error(data_dir: Path) -> None:
    """A 0-qty ``DeckCard`` is a ValidationError (``Field(ge=1)``)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DeckCard(name='Sol Ring', quantity=0)


def test_deckcard_quantity_negative_is_validation_error(data_dir: Path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DeckCard(name='Sol Ring', quantity=-1)


def test_deck_remove_negative_qty_refused_no_grow(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """``deck-remove --qty -1`` is refused and does NOT grow the deck (M5 core repro)."""
    cards = _commander_cards()
    cards[1] = {'name': 'Forest', 'quantity': 5}  # a real multi-copy entry to target.
    _save_source_deck(monkeypatch, tmp_path, 'Gruul', cards)
    capsys.readouterr()

    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, 'deck-remove', 'Gruul', 'Forest', '--qty', '-1')
    assert ei.value.code != 0
    err = capsys.readouterr().err
    assert 'Traceback' not in err

    _run(monkeypatch, 'get-deck', 'Gruul')
    deck = json.loads(capsys.readouterr().out)
    forest = next(c for c in deck['cards'] if c['name'] == 'Forest')
    assert forest['quantity'] == 5  # unchanged — the deck did NOT grow.


def test_deck_add_zero_qty_refused(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """``deck-add --qty 0`` is refused (never lands a 0-qty entry)."""
    _save_source_deck(monkeypatch, tmp_path, 'Gruul', _commander_cards())
    capsys.readouterr()

    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, 'deck-add', 'Gruul', 'Sol Ring', '--qty', '0')
    assert ei.value.code != 0
    err = capsys.readouterr().err
    assert 'Traceback' not in err

    _run(monkeypatch, 'get-deck', 'Gruul')
    deck = json.loads(capsys.readouterr().out)
    assert not any(c['name'] == 'Sol Ring' for c in deck['cards'])


# --------------------------------------------------------------------------- #
# m2 — remove_card refuses cutting the sole commander
# --------------------------------------------------------------------------- #


def test_deck_remove_sole_commander_refused(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """``deck-remove`` on the sole commander is refused; the commander survives."""
    _save_source_deck(monkeypatch, tmp_path, 'Gruul', _commander_cards('Grumgully, the Generous'))
    capsys.readouterr()

    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, 'deck-remove', 'Gruul', 'Grumgully, the Generous')
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith('error: ')
    assert 'Traceback' not in err

    _run(monkeypatch, 'get-deck', 'Gruul')
    deck = json.loads(capsys.readouterr().out)
    assert any(
        c['name'] == 'Grumgully, the Generous' and c['role'] == 'commander' for c in deck['cards']
    )


def test_remove_card_sole_commander_refused_store_level(data_dir: Path) -> None:
    """The store guard itself refuses the sole-commander cut (not just the CLI)."""
    from pipeline.decks.store import DecksError

    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    with pytest.raises(DecksError):
        s.remove_card('d1', 'Krenko, Mob Boss')
    assert any(c.role == 'commander' for c in _get(s, 'd1').cards)


# --------------------------------------------------------------------------- #
# m1 — commander never reaches qty 2 (add / swap increment hole closed)
# --------------------------------------------------------------------------- #


def test_add_card_cannot_increment_existing_commander(data_dir: Path) -> None:
    """``add_card`` refuses incrementing an existing commander to qty 2."""
    from pipeline.decks.store import DecksError

    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    with pytest.raises(DecksError):
        s.add_card('d1', DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander'))
    cmd = next(c for c in _get(s, 'd1').cards if c.role == 'commander')
    assert cmd.quantity == 1  # still a singleton.


def test_swap_cannot_increment_existing_commander(data_dir: Path) -> None:
    """``swap`` refuses re-adding the existing commander (the m1 exemption hole)."""
    from pipeline.decks.store import DecksError

    s = DecksStore()
    s.put(_deck(), deck_uuid='d1')
    # Cut a filler, add back the commander name -> would push the commander to qty 2.
    with pytest.raises(DecksError):
        s.swap(
            'd1',
            add=DeckCard(name='Krenko, Mob Boss', quantity=1, role='commander'),
            cut='Goblin 0',
        )
    cmd = next(c for c in _get(s, 'd1').cards if c.role == 'commander')
    assert cmd.quantity == 1


# --------------------------------------------------------------------------- #
# M3 — boundary canonicalization (REAL resolver): one entry, still one after re-pull
# --------------------------------------------------------------------------- #


def test_deck_add_canonicalizes_to_single_entry(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """A deck holding canonical ``Lightning Bolt`` + a ``lightning bolt`` add = ONE entry.

    Uses the REAL ``default_card_resolver()`` (a real lake row canonicalizes the
    lowercase add) — never a stub. And after a push + re-pull the entry is STILL
    one (the local name matches the source canonicalization, so no dup hydrates).
    """
    _land_canonical_card('Lightning Bolt', 'llll-bolt-oracle')

    cards = _commander_cards()
    cards[1] = {'name': 'Lightning Bolt'}  # canonical entry already in the deck.
    _save_source_deck(monkeypatch, tmp_path, 'Gruul', cards)
    capsys.readouterr()

    # Add the LOWERCASE variant — canonicalization must merge into the existing entry.
    _run(monkeypatch, 'deck-add', 'Gruul', 'lightning bolt', '--qty', '1')
    capsys.readouterr()

    _run(monkeypatch, 'get-deck', 'Gruul')
    deck = json.loads(capsys.readouterr().out)
    bolts = [c for c in deck['cards'] if c['name'].lower() == 'lightning bolt']
    assert len(bolts) == 1  # ONE entry, not two.
    assert bolts[0]['name'] == 'Lightning Bolt'  # canonical spelling.
    assert bolts[0]['quantity'] == 2

    # Re-pull from the source: still ONE entry (no duplicate singleton hydrates).
    _run(monkeypatch, 'pull', 'Gruul')
    capsys.readouterr()
    _run(monkeypatch, 'get-deck', 'Gruul')
    deck = json.loads(capsys.readouterr().out)
    bolts = [c for c in deck['cards'] if c['name'].lower() == 'lightning bolt']
    assert len(bolts) == 1
    assert bolts[0]['name'] == 'Lightning Bolt'


def test_deck_add_unresolved_name_passes_through_verbatim(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """An UNRESOLVED name (spoiler/unreleased) passes through raw — honest, not dropped.

    The data_dir fixture's REAL resolver 404s the live fallback for an unseeded
    name, so it returns None and the raw name is kept verbatim (the resolving-card-
    names lesson: a Scryfall miss is not a fake). Still the real resolver — offline.
    """
    _save_source_deck(monkeypatch, tmp_path, 'Gruul', _commander_cards())
    capsys.readouterr()

    _run(monkeypatch, 'deck-add', 'Gruul', 'Totally Unreleased Spoiler Card', '--qty', '1')
    capsys.readouterr()

    _run(monkeypatch, 'get-deck', 'Gruul')
    deck = json.loads(capsys.readouterr().out)
    assert any(c['name'] == 'Totally Unreleased Spoiler Card' for c in deck['cards'])


# --------------------------------------------------------------------------- #
# M5-tail — a refused commit auto-undoes the local edit (never half-lands)
# --------------------------------------------------------------------------- #


def test_refused_commit_restores_pre_edit_deck(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """A synced edit whose commit-push is refused (shrink) restores the pre-edit deck.

    The commit is refused by making the store's push raise (drift/shrink). The edit
    must NOT half-land: after the failed verb the local deck equals its pre-edit
    content (the add was rolled back), so a later verb is not poisoned.
    """
    _save_source_deck(monkeypatch, tmp_path, 'Gruul', _commander_cards())
    capsys.readouterr()

    from pipeline.decks import sync as sync_mod

    before_version: dict[str, str] = {}
    s = DecksStore()
    uuid = s.uuid_for_name('Gruul')
    assert uuid is not None
    before_version['v'] = version(_get(s, uuid))

    # Force the commit-push to be refused.
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise sync_mod.SyncDriftError('source moved under us; refusing to clobber')

    monkeypatch.setattr('pipeline.decks.sync.push', _boom)

    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, 'deck-add', 'Gruul', 'Sol Ring', '--qty', '1')
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert 'Traceback' not in err

    # The local edit was rolled back — the deck is pre-edit content, not shrunk/grown.
    after = DecksStore().get(uuid)
    assert after is not None
    assert not any(c.name == 'Sol Ring' for c in after.cards)
    assert version(after) == before_version['v']


# --------------------------------------------------------------------------- #
# M1 — undo cursor: 3 edits then 3 undos reveal 3 prior states, no oscillation
# --------------------------------------------------------------------------- #


def test_undo_cursor_walks_back_three_distinct_states(data_dir: Path) -> None:
    """3 sequential edits, then 3 undos -> the 3 prior states in reverse (no oscillation)."""
    s = DecksStore()
    s.put(_deck(strategy='s0'), deck_uuid='d1')
    s.set_strategy('d1', 's1')
    s.set_strategy('d1', 's2')
    s.set_strategy('d1', 's3')
    assert _get(s, 'd1').strategy == 's3'

    # Each undo (a fresh store instance = a fresh process) steps back one DISTINCT state.
    assert DecksStore().undo('d1') is not None
    assert _get(DecksStore(), 'd1').strategy == 's2'
    assert DecksStore().undo('d1') is not None
    assert _get(DecksStore(), 'd1').strategy == 's1'
    assert DecksStore().undo('d1') is not None
    assert _get(DecksStore(), 'd1').strategy == 's0'


def test_undo_cursor_no_oscillation_between_two(data_dir: Path) -> None:
    """Two undos never oscillate back to the newer state (the OFFSET-1 bug)."""
    s = DecksStore()
    s.put(_deck(strategy='a'), deck_uuid='d1')
    s.set_strategy('d1', 'b')
    DecksStore().undo('d1')
    assert _get(DecksStore(), 'd1').strategy == 'a'
    # A second undo has nothing older to go to (floor) — must NOT flip back to 'b'.
    DecksStore().undo('d1')
    assert _get(DecksStore(), 'd1').strategy != 'b'


def test_undo_skips_identical_content_no_deadlock(data_dir: Path) -> None:
    """A re-applied identical edit does NOT deadlock undo (skips equal-content versions)."""
    s = DecksStore()
    s.put(_deck(strategy='orig'), deck_uuid='d1')
    s.set_strategy('d1', 'changed')
    # Re-apply the SAME set-strategy twice (identical content, back-to-back).
    s.set_strategy('d1', 'changed')
    assert _get(s, 'd1').strategy == 'changed'

    # Undo must skip the identical 'changed' versions and reach the genuinely prior 'orig'.
    restored = DecksStore().undo('d1')
    assert restored is not None
    assert _get(DecksStore(), 'd1').strategy == 'orig'


def test_new_edit_resets_cursor_to_head_no_redo(data_dir: Path) -> None:
    """A new edit after an undo resets the cursor to head (no redo; forward abandoned)."""
    s = DecksStore()
    s.put(_deck(strategy='s0'), deck_uuid='d1')
    s.set_strategy('d1', 's1')
    s.set_strategy('d1', 's2')
    DecksStore().undo('d1')  # -> s1
    assert _get(DecksStore(), 'd1').strategy == 's1'

    # A NEW edit branches from s1; the cursor resets to head.
    DecksStore().set_strategy('d1', 's3')
    assert _get(DecksStore(), 'd1').strategy == 's3'
    # Undo now steps back from s3 -> s1 (the state the new edit branched from), NOT s2.
    DecksStore().undo('d1')
    assert _get(DecksStore(), 'd1').strategy == 's1'
