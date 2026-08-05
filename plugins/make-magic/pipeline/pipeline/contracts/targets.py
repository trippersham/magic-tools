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

__all__ = ('target_for_format',)

#: Sixty-card constructed formats keyed by their normalized (casefolded) name.
_SIXTY_CARD_FORMATS: frozenset[str] = frozenset({'standard', 'modern', 'pioneer', 'brawl', 'historic', 'pauper'})


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
    if fmt is None:
        return None
    norm = fmt.strip().casefold()
    if not norm:
        return None
    if 'commander' in norm or norm == 'edh':
        return 100
    if norm in _SIXTY_CARD_FORMATS:
        return 60
    return None
