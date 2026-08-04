"""The deck ACCESS path — route collection deck reads/writes through the local store.

Design §2/§8 + W1/W4: ALL deck access routes through the local ``DecksStore``,
but ONLY decks (inventory / chase / trades keep calling ``get_store`` directly —
they were never the brittle part and are NOT rerouted here).

Pull policy (W4 — the chosen policy):
    A SYNCED deck is pulled-current from its source on FIRST access (the local
    copy is absent) OR when a short TTL has elapsed since the last pull; the local
    copy is served thereafter. An EPHEMERAL deck is served straight from the local
    store (no source, zero network). Edits go to the LOCAL store; a PUSH to the
    source happens only at an explicit commit boundary (the ``push`` / ``sync``
    verbs), never on every edit.

This keeps a plain ``get-deck`` from going stale against the source without a
network round-trip on every read: the TTL bounds staleness while amortizing the
pull. A manual ``pull`` verb forces a refresh when the user wants one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pipeline.decks import sync as _sync
from pipeline.decks.store import DecksStore
from pipeline.decks.version import version

if TYPE_CHECKING:
    from pipeline.collection.store import CollectionStore
    from pipeline.contracts import Deck

__all__ = ('DeckAccess', 'deck_access', 'default_deck_id')

#: How long a synced local copy is served before a read re-pulls it (W4 TTL).
_PULL_TTL = timedelta(minutes=15)


def default_deck_id(backend: str, name: str) -> str:
    """The local ``deck_id`` for a synced deck: ``'<backend>:<name>'``.

    Namespaced by backend so the same deck name under two backends never collides
    in the local store.
    """
    return f'{backend}:{name}'


class DeckAccess:
    """Route deck reads/writes through the local ``DecksStore`` over a source driver.

    Decks only (W1). The ``driver`` is the active source-of-record
    ``CollectionStore`` (built by ``get_store``); inventory/chase/trades are NOT
    handled here — callers use the driver directly for those.
    """

    def __init__(self, driver: CollectionStore, *, decks: DecksStore | None = None) -> None:
        self._driver = driver
        self._decks = decks if decks is not None else DecksStore()

    @property
    def backend(self) -> str:
        return self._driver.backend_name

    def deck_id_for(self, name: str) -> str:
        return default_deck_id(self.backend, name)

    # ----------------------------------------------------------------------- #
    # Read (pull policy)
    # ----------------------------------------------------------------------- #

    def _needs_pull(self, deck_id: str) -> bool:
        """True when a synced deck should be (re-)pulled: absent OR TTL elapsed."""
        row = self._decks.get_row(deck_id)
        if row is None or self._decks.get(deck_id) is None:
            return True  # first access — no local copy yet.
        pulled_at = self._pulled_at(row.freshness)
        if pulled_at is None:
            return True
        return datetime.now(tz=UTC) - pulled_at >= _PULL_TTL

    @staticmethod
    def _pulled_at(freshness: str | None) -> datetime | None:
        """Parse the ``pulled_at`` stamp out of the local-only ``freshness`` JSON."""
        if not freshness:
            return None
        try:
            data = json.loads(freshness)
        except (json.JSONDecodeError, TypeError):
            return None
        raw = data.get('pulled_at') if isinstance(data, dict) else None
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def _stamp_pulled(self, deck_id: str) -> None:
        """Record the pull time in the local-only ``freshness`` column."""
        self._decks.set_freshness(deck_id, {'pulled_at': datetime.now(tz=UTC).isoformat()})

    def read_deck(self, name: str) -> Deck:
        """Return the deck for ``name``, applying the pull policy (W4).

        Synced + (absent or stale) -> pull from the source, serve the fresh local
        copy. Synced + fresh -> serve the local copy. If the deck is not yet known
        locally, it is treated as synced and pulled from the source (``get-deck``
        of a source deck that was never opened locally).
        """
        deck_id = self.deck_id_for(name)
        row = self._decks.get_row(deck_id)
        # Ephemeral decks (no source) are served straight from local.
        if row is not None and row.sync_status == 'ephemeral':
            local = self._decks.get(deck_id)
            if local is not None:
                return local
        if self._needs_pull(deck_id):
            _sync.pull(self._decks, self._driver, deck_id=deck_id, source_ref=name)
            self._stamp_pulled(deck_id)
        local = self._decks.get(deck_id)
        if local is None:  # pragma: no cover - pull just wrote it.
            return self._driver.get_deck(name)
        return local

    # ----------------------------------------------------------------------- #
    # Write (local edits; push at the explicit commit boundary)
    # ----------------------------------------------------------------------- #

    def save_deck(self, deck: Deck, *, allow_shrink: bool = False, commit: bool = True) -> None:
        """Write ``deck`` to the local store; PUSH to the source at the commit boundary.

        ``save-deck`` is the user's AUTHORITATIVE write (arbitrary deck JSON), so it
        adopts the current source version as the baseline — the push is treated as
        our own authoritative write, not a drift (the shrink ceremony still guards
        it). Set ``commit=False`` to stage the local edit without touching the
        source.
        """
        deck_id = self.deck_id_for(deck.name)
        # Baseline := the current source version if it exists, else None (a create).
        # This makes the subsequent push our authoritative write (no false drift),
        # while the source's shrink ceremony still fires on commit.
        try:
            baseline = version(self._driver.get_deck(deck.name))
        except FileNotFoundError:
            baseline = None
        self._decks.put(deck, deck_id=deck_id, sync_status='synced', source_ref=deck.name,
                        synced_baseline=baseline, rationale='save-deck')
        if commit:
            self.push(deck.name, allow_shrink=allow_shrink)

    def set_strategy(self, name: str, text: str, *, commit: bool = True) -> None:
        self._ensure_local(name)
        self._decks.set_strategy(self.deck_id_for(name), text, rationale='set-strategy')
        if commit:
            self.push(name)

    def set_assessment(self, name: str, text: str, *, commit: bool = True) -> None:
        self._ensure_local(name)
        self._decks.set_assessment(self.deck_id_for(name), text, rationale='set-assessment')
        if commit:
            self.push(name)

    def set_focus_otags(self, name: str, otags: list[str], *, commit: bool = True) -> None:
        self._ensure_local(name)
        self._decks.set_focus_otags(self.deck_id_for(name), list(otags), rationale='set-focus-otags')
        if commit:
            self.push(name)

    def _ensure_local(self, name: str) -> None:
        """Make sure a local copy exists (pull it current) before a typed edit."""
        self.read_deck(name)

    # ----------------------------------------------------------------------- #
    # Manual sync verbs
    # ----------------------------------------------------------------------- #

    def pull(self, name: str) -> None:
        """Force a pull of ``name`` from the source into the local store."""
        deck_id = self.deck_id_for(name)
        _sync.pull(self._decks, self._driver, deck_id=deck_id, source_ref=name)
        self._stamp_pulled(deck_id)

    def push(self, name: str, *, allow_shrink: bool = False) -> None:
        """Push the local deck ``name`` to the source through the ceremony (guarded)."""
        _sync.push(self._decks, self._driver, deck_id=self.deck_id_for(name), allow_shrink=allow_shrink)

    def sync(self, name: str, *, allow_shrink: bool = False) -> None:
        """Pull-then-push ``name`` (reconcile local against the source)."""
        self.pull(name)
        self.push(name, allow_shrink=allow_shrink)


def deck_access(driver: CollectionStore, *, decks: DecksStore | None = None) -> DeckAccess:
    """Construct a :class:`DeckAccess` over the given source driver (decks only)."""
    return DeckAccess(driver, decks=decks)
