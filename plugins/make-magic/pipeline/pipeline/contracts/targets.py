"""Deck format -> target-size mapping (the single source of truth for a target).

A deck's target size is a pure function of its declared format. The mapping is
deliberately tolerant: the live Airtable ``Format`` field's exact string values
are human-owned and not yet pinned down (could be ``'Commander'``, ``'EDH'``,
``'Duel Commander'``, ...), so :func:`target_for_format` normalizes (strip +
casefold) and matches on substring/equality rather than an exact enum. An empty
or unknown format yields ``None`` (untargeted) so the audit layer treats such a
deck as WIP rather than a violation.
"""

from __future__ import annotations

__all__ = ('is_commander_format', 'target_for_format')

#: Sixty-card constructed formats keyed by their normalized (casefolded) name.
_SIXTY_CARD_FORMATS: frozenset[str] = frozenset({'standard', 'modern', 'pioneer', 'brawl', 'historic', 'pauper'})


def is_commander_format(fmt: str | None) -> bool:
    """The single canonical Commander/EDH format test — the source of truth every caller shares.

    Tolerant by the same rule :func:`target_for_format` uses for its 100-card branch (strip +
    casefold, then substring ``'commander'`` or ``== 'edh'``), so ``'Duel Commander'`` and
    ``'EDH'`` both qualify and the two functions can never drift. An empty/``None`` format is
    NOT commander-format (it is *undeclared* — see ``Deck.expects_commander`` for the
    Commander-centric import default).

    Args:
        fmt: The deck's declared format string (human-owned), or ``None``.
    """
    if not fmt:
        return False
    norm = fmt.strip().casefold()
    if 'non-commander' in norm or 'not commander' in norm:
        return False
    return 'commander' in norm or norm == 'edh'


def target_for_format(fmt: str | None) -> int | None:
    """Return the target deck size for a declared format, or ``None`` if untargeted.

    The match is tolerant: input is stripped and casefolded, then anything
    containing ``'commander'`` (or equal to ``'edh'``) maps to 100, and the known
    sixty-card constructed formats map to 60. Empty or unrecognized input returns
    ``None`` so the caller treats the deck as untargeted (WIP), not a violation.

    Args:
        fmt: The deck's declared format string (human-owned), or ``None``.

    Returns:
        ``100`` for Commander/EDH, ``60`` for the sixty-card formats, else ``None``.
    """
    if is_commander_format(fmt):
        return 100
    norm = (fmt or '').strip().casefold()
    if norm in _SIXTY_CARD_FORMATS:
        return 60
    return None
