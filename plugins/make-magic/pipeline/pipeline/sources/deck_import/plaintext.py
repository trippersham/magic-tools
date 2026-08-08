"""Plaintext / Forge ``.dck`` deck-import adapter (Phase 1).

The offline importer: it turns a decklist you already have — a file path, ``-``
(stdin), or raw pasted text — into a :class:`~pipeline.sources.deck_import.raw.RawDeck`.
It is deliberately tolerant, accepting the several list dialects that show up in
the wild through one unified line parser:

    - quantity lines: ``1 Sol Ring`` / ``1x Sol Ring`` / ``4 Lightning Bolt``; a
      bare ``Sol Ring`` (no count) -> quantity 1;
    - section headers that set the current role: ``Commander:`` / ``[Commander]``
      -> commander; ``Sideboard:`` / ``[Sideboard]`` -> sideboard; ``Deck`` /
      ``Maindeck`` / ``[Main]`` / a blank line -> maindeck (``None``);
    - Forge ``.dck`` INI: ``[metadata]`` (``Name=`` grabbed, rest ignored),
      ``[Commander]`` / ``[Main]`` / ``[Sideboard]`` — the same header machinery;
    - the Moxfield export inline marker: a line ending ``*CMDR*`` promotes that card
      to commander; other ``*..*`` tags (``*F*`` foil, set/collector tags) are
      stripped from the name;
    - blank lines and ``//`` / ``#`` comments are ignored.

Paste-sourced imports are PERMANENT in the cache (keyed by a content hash): once
parsed, identical text is served without re-parsing until an explicit
``refresh``/``invalidate``. ``normalize`` delegates to the shared
:func:`~pipeline.sources.deck_import._normalize_rawdeck` — this adapter's work is
all in the parser.

Deck name: from the ``.dck`` ``Name=`` when present; otherwise the constant
``'Imported deck'`` (the CLI supplies ``--name``; keeping a stable default here
means the parser never has to guess a name from the card list).
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline.collection.errors import CollectionError
from pipeline.contracts import ROLE_COMMANDER, ROLE_SIDEBOARD
from pipeline.sources.deck_import.raw import RawDeck, RawEntry, content_key, load_or_fetch

if TYPE_CHECKING:
    from pipeline.contracts import Deck

__all__ = ('PlaintextImporter',)

#: Deck-host URLs that later phases (P2-P4) own; this adapter must NOT claim them.
#: Small + hardcoded on purpose — the registry, not a URL guess, decides the API
#: sources. Anything else that looks like a decklist is ours.
_HOST_EXCLUSIONS = ('archidekt.com', 'edhrec.com', 'moxfield.com')

#: The stdin sentinel: ``import-deck -`` reads the deck text from standard input.
_STDIN_SENTINEL = '-'

#: Default deck name when no source name is available (``.dck`` ``Name=`` wins).
_DEFAULT_NAME = 'Imported deck'

#: A quantity line: an optional leading ``N`` / ``Nx`` count, then the card name.
_QTY_RE = re.compile(r'^(?P<qty>\d+)\s*x?\s+(?P<name>.+?)\s*$', re.IGNORECASE)
#: A bracket section header, e.g. ``[Commander]`` / ``[Main]`` / ``[metadata]``.
_BRACKET_RE = re.compile(r'^\[(?P<label>[^\]]+)\]\s*$')
#: An INI ``key=value`` line (used to grab ``Name=`` from ``.dck`` ``[metadata]``).
_KV_RE = re.compile(r'^(?P<key>[A-Za-z][\w ]*?)\s*=\s*(?P<value>.*)$')
#: A trailing / inline ``*TAG*`` marker (Moxfield ``*CMDR*``, ``*F*``, set tags).
_MARKER_RE = re.compile(r'\*[^*]+\*')
#: The Moxfield commander marker (matched case-insensitively, post-strip).
_CMDR_MARKER = 'cmdr'

#: Header labels (lowercased, ``:``/brackets stripped) that set the current role.
_COMMANDER_HEADERS = frozenset({'commander', 'commanders', 'command zone'})
_SIDEBOARD_HEADERS = frozenset({'sideboard', 'maybeboard'})
_MAINDECK_HEADERS = frozenset({'deck', 'maindeck', 'main', 'mainboard', 'metadata'})


def _looks_like_prose(name: str) -> bool:
    """True if a bare (count-less) line reads like a sentence, not a card name.

    A count-less line is only accepted as a card when it plausibly names one. This
    keeps a prose blob ("This is just some prose...") from being mis-parsed into
    cards while still accepting a bare ``Sol Ring``. Heuristic (bare lines only —
    a line with an explicit ``N`` count is always a card): reject if it ends with
    sentence punctuation, carries a mid-line sentence mark, or is implausibly long
    / many-worded for a card name.
    """
    if name.endswith(('.', ',', ':', ';', '!', '?')):
        return True
    if re.search(r'[.;!?]\s', name):
        return True
    return len(name) > 60 or len(name.split()) > 8


class PlaintextImporter:
    """Import a decklist from a file / ``-`` (stdin) / pasted text (offline)."""

    source = 'plaintext'

    def matches(self, ref: str) -> bool:
        """True iff ``ref`` is a decklist we can read offline (not a known host URL).

        False for the deck-host URLs later adapters own (:data:`_HOST_EXCLUSIONS`)
        and for a bare all-digits ref (Archidekt's numeric-id addressing form — a
        card name is never purely numeric, so this can only be a deck id).
        Otherwise True for: the ``-`` stdin sentinel; an existing file path; a path
        ending ``.dck``; or a raw multi-line string that contains a card-line
        pattern (a quantity line or a bare, non-prose card name).
        """
        lowered = ref.lower()
        if any(host in lowered for host in _HOST_EXCLUSIONS):
            return False
        if ref.strip().isdigit():
            return False  # a bare numeric id is Archidekt's addressing form, never a decklist
        if ref == _STDIN_SENTINEL:
            return True
        if lowered.endswith('.dck'):
            return True
        try:
            if Path(ref).is_file():
                return True
        except OSError:
            pass
        return self._has_card_line(ref)

    def fetch(self, ref: str, *, refresh: bool = False) -> RawDeck:
        """Read ``ref`` -> parse -> a cached, PERMANENT ``RawDeck`` (paste-sourced).

        ``ref`` is read as: stdin (``-``), a file's text (an existing/`.dck` path),
        or — failing that — the deck text itself. Parsing is routed through
        :func:`~pipeline.sources.deck_import.raw.load_or_fetch` keyed by the text's
        :func:`~pipeline.sources.deck_import.raw.content_key`, ``permanent=True`` so
        identical text is never re-parsed until ``refresh``/``invalidate``.
        """
        text = self._read_text(ref)
        key = content_key(text)

        def _do_parse() -> RawDeck:
            entries, name = self._parse(text)
            if not entries:
                raise CollectionError('no card lines found in the pasted/loaded deck')
            return RawDeck(
                name=name or _DEFAULT_NAME,
                cards=entries,
                source=self.source,
                source_ref=key,
                fetched_at=datetime.now(tz=UTC),
            )

        return load_or_fetch(self.source, key, _do_parse, refresh=refresh, permanent=True)

    def normalize(self, raw: RawDeck) -> Deck:
        """Turn the ``RawDeck`` into a canonical ``Deck`` via the shared helper."""
        from pipeline.sources.deck_import import _normalize_rawdeck

        return _normalize_rawdeck(raw)

    # ----------------------------------------------------------------------- #
    # Reading + parsing
    # ----------------------------------------------------------------------- #

    def _read_text(self, ref: str) -> str:
        """Resolve ``ref`` to the raw deck text (stdin / file / the ref itself)."""
        if ref == _STDIN_SENTINEL:
            return sys.stdin.read()
        try:
            path = Path(ref)
            if path.is_file():
                return path.read_text(encoding='utf-8')
        except OSError:
            pass
        return ref

    def _has_card_line(self, text: str) -> bool:
        """True iff ``text`` has at least one parseable card line (for ``matches``)."""
        entries, _name = self._parse(text)
        return bool(entries)

    def _parse(self, text: str) -> tuple[list[RawEntry], str | None]:
        """Parse deck text into ``(entries, name)`` — the whole tolerant parser.

        Walks lines top-to-bottom tracking the current role (set by section
        headers); ``[metadata]`` ``Name=`` becomes the deck name. Both the plaintext
        section forms (``Commander:`` / ``[Commander]`` / ``Deck``) and the Forge
        ``.dck`` INI sections flow through the same header handling.
        """
        entries: list[RawEntry] = []
        name: str | None = None
        current_role: str | None = None
        in_metadata = False

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                current_role = None  # a blank line ends a role section (back to maindeck)
                continue
            if line.startswith(('//', '#')):
                continue

            header = self._section_role(line)
            if header is not None:
                current_role, in_metadata = header
                continue

            if in_metadata:
                kv = _KV_RE.match(line)
                if kv is not None and kv.group('key').strip().lower() == 'name':
                    name = kv.group('value').strip() or None
                continue

            entry = self._card_line(line, current_role)
            if entry is not None:
                entries.append(entry)

        return entries, name

    def _section_role(self, line: str) -> tuple[str | None, bool] | None:
        """Map a header line to ``(role, in_metadata)``, or ``None`` if not a header.

        Handles both ``[Bracket]`` headers (INI + ``[Commander]`` plaintext) and the
        ``Label:`` / bare ``Label`` plaintext forms. Returns ``None`` when the line
        is not a recognized section header (so it falls through to card parsing) —
        distinct from a header whose ``role`` element is ``None`` (a maindeck
        header). ``in_metadata`` is True only inside a ``.dck`` ``[metadata]`` block.
        """
        bracket = _BRACKET_RE.match(line)
        if bracket is not None:
            return self._role_for_label(bracket.group('label').strip().lower())
        # A bare / colon-terminated header, e.g. ``Commander:`` or ``Sideboard``.
        # Only a lone header word (optionally ``:``-terminated) counts, never a
        # card line like ``1 Commander's Sphere``.
        candidate = line.rstrip(':').strip().lower()
        if candidate in _COMMANDER_HEADERS or candidate in _SIDEBOARD_HEADERS or candidate in _MAINDECK_HEADERS:
            return self._role_for_label(candidate)
        return None

    def _role_for_label(self, label: str) -> tuple[str | None, bool]:
        """Resolve a normalized header ``label`` to ``(role, in_metadata)``."""
        if label == 'metadata':
            return None, True
        if label in _COMMANDER_HEADERS:
            return ROLE_COMMANDER, False
        if label in _SIDEBOARD_HEADERS:
            return ROLE_SIDEBOARD, False
        return None, False  # maindeck headers (and any other bracketed section we treat as main)

    def _card_line(self, line: str, role: str | None) -> RawEntry | None:
        """Parse one card line into a ``RawEntry`` (role from the section), or ``None``.

        Strips ``*TAG*`` markers first; a ``*CMDR*`` marker promotes the line to
        commander regardless of the section. A leading ``N`` / ``Nx`` count sets the
        quantity; a bare, non-prose line is quantity 1. Returns ``None`` for a line
        that is not a plausible card.
        """
        markers = [m.group(0).strip('*').strip().lower() for m in _MARKER_RE.finditer(line)]
        stripped = _MARKER_RE.sub('', line).strip()
        if not stripped:
            return None
        effective_role = ROLE_COMMANDER if _CMDR_MARKER in markers else role

        match = _QTY_RE.match(stripped)
        if match is not None:
            return RawEntry(name=match.group('name').strip(), quantity=int(match.group('qty')), role=effective_role)
        if _looks_like_prose(stripped):
            return None
        return RawEntry(name=stripped, quantity=1, role=effective_role)
