"""Filesystem-safe deck identifiers for staging a rendered deck to disk.

A deck's human name (Airtable primary field) is arbitrary text — it can contain
``/`` (``U/R Izzet``), ``:``, or other path-illegal characters, and it changes
over time. When a backend stages a deck to a file for a downstream tool (e.g. the
Forge ``sim`` runner writes ``<name>.dck`` into Forge's decks dir), using the raw
name as the filename breaks: ``U/R Izzet`` becomes a ``U/`` sub-directory + a
missing file. The display name still belongs in the file's ``Name=`` metadata —
only the on-disk stem needs sanitizing. This is that (owned by the destination
layer, per design, so every consumer sanitizes identically).
"""

from __future__ import annotations

import re

__all__ = ('safe_deck_stem',)

#: Characters we keep in a stem: word chars (letters/digits/underscore, unicode),
#: space, dash, dot, parentheses. Everything else (``/ : * ? " < > |`` control
#: chars, …) collapses to ``_``.
_UNSAFE = re.compile(r'[^\w.\-() ]+', re.UNICODE)
_WS = re.compile(r'\s+')
#: Bound the stem so a very long deck name can't blow a filesystem path limit.
_MAX_LEN = 100


def safe_deck_stem(name: str) -> str:
    """Derive a filesystem-safe filename stem from a deck ``name``.

    Replaces path-illegal / control characters with ``_``, collapses runs of
    whitespace to a single space, strips leading/trailing dots+spaces (which
    Windows rejects and which read badly), bounds the length, and never returns
    an empty string (falls back to ``deck``). The transform is deterministic and
    idempotent; the human name is preserved separately (in the ``.dck`` ``Name=``
    line), so this only governs the on-disk stem.

    Note: two different names can sanitize to the same stem (e.g. ``A/B`` and
    ``A:B`` -> ``A_B``); callers that stage two decks at once must disambiguate
    a collision themselves (the sim runner appends distinct suffixes).
    """
    stem = _UNSAFE.sub('_', name)
    stem = _WS.sub(' ', stem).strip()
    stem = stem.strip('. ')[:_MAX_LEN].strip('. ')
    # Fall back to a readable default when nothing meaningful survives (empty, or
    # only separators like '///' -> '_'): a stem needs at least one alphanumeric.
    if not any(ch.isalnum() for ch in stem):
        return 'deck'
    return stem
