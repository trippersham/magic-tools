"""A membership oracle over Forge's real card set — the data-driven availability
check that makes deck→Forge validation factual instead of a guess.

Before this, nothing told us which of Forge's ~33k cards a name actually maps to,
so a card absent from Forge silently produced a short/mis-loaded deck, and (worse)
a human "is this loadable?" guess led to a wrong DFC substitution. :class:`ForgeCardIndex`
answers :meth:`has` from the install's ``cardsfolder.zip`` — built once and cached
to a per-install manifest, normalizing the combined ``A // B`` DFC name to its
front face exactly the way Forge's own deck loader resolves it (empirically:
Forge loads an MDFC by BOTH the combined name and the front face).

It implements the structural ``CardAvailability`` port the forge_dck card exporter
depends on (duck-typed — no import back into the destination layer), so unit tests
inject a tiny hand-built index and never need a real Forge.
"""

from __future__ import annotations

import contextlib
import unicodedata
import zipfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from pipeline.sim.forge_runtime import ForgeInstall

__all__ = ('ForgeCardIndex',)


def _norm(name: str) -> str:
    """Canonicalize a card name for membership: NFC-normalize, then strip + lower.

    Forge's cardsfolder ``Name:`` lines and Scryfall names can differ in Unicode
    normalization FORM (composed vs decomposed) for accented cards — Lim-Dûl's
    Vault, Jötun Grunt, Dandân, Márton Stromgald, Séance. Two forms of the same
    name compare unequal as raw strings, so without a shared normal form a VALID
    card is misclassified ABSENT_FROM_TARGET and the sim hard-fails a good deck.
    Applying ``NFC`` on BOTH the index build and every lookup makes the match
    normalization-insensitive.
    """
    return unicodedata.normalize('NFC', name).strip().lower()


#: Basic lands are always loadable in Forge and are frequently the bulk of a deck;
#: guarantee them present so an index (real or a test fake) never flags a basic.
_BASICS: frozenset[str] = frozenset(
    _norm(f'{prefix}{land}')
    for land in ('plains', 'island', 'swamp', 'mountain', 'forest', 'wastes')
    for prefix in ('', 'snow-covered ')
)

#: The cached name manifest lives beside the install so it is built once per
#: provisioned Forge (the dir is already version-specific).
_MANIFEST_NAME = 'card_names.manifest'


class ForgeCardIndex:
    """Case-insensitive membership over Forge's card names, DFC-aware.

    Construct directly from a name set (tests), or via :meth:`from_install` /
    :meth:`from_zip` to build+cache from a real ``cardsfolder.zip``.
    """

    def __init__(self, names: frozenset[str]) -> None:
        #: NFC-normalized + lowercased for case/normalization-insensitive lookup;
        #: basics folded in defensively.
        self._names: frozenset[str] = frozenset(_norm(n) for n in names) | _BASICS

    def has(self, card_name: str) -> bool:
        """Whether Forge can load ``card_name`` (case-insensitive, DFC-normalized).

        A combined ``A // B`` name matches when the FRONT face ``A`` is known —
        the way Forge itself resolves an MDFC in a deck line — so DFCs/MDFCs
        (``Akoum Warrior // Akoum Teeth``) validate as present, not absent.
        """
        n = _norm(card_name)
        if n in self._names:
            return True
        if ' // ' in n:
            front = n.split(' // ', 1)[0].strip()
            if front in self._names:
                return True
        return False

    def __len__(self) -> int:
        return len(self._names)

    # ----------------------------------------------------------------------- #
    # Builders (cache-backed; not exercised by the offline suite).
    # ----------------------------------------------------------------------- #

    @classmethod
    def from_install(cls, install: ForgeInstall) -> ForgeCardIndex:
        """Build (or load a cached) index for a resolved Forge install.

        Reads/writes ``<forge_dir>/card_names.manifest``: on a cache hit the
        manifest is loaded directly; otherwise the ``cardsfolder.zip`` is parsed
        once and the result cached. Falls back to a fresh parse if the manifest
        is unreadable.

        Note: the manifest is keyed only by ``forge_dir``, so an IN-PLACE
        ``cardsfolder.zip`` update under the same dir will NOT trigger a rebuild.
        That is acceptable because provisioned installs are version-specific dirs
        (a Forge upgrade lands in a new dir → a fresh manifest); delete the
        manifest to force a rebuild after an in-place swap.
        """
        forge_dir = install.forge_dir
        manifest = forge_dir / _MANIFEST_NAME
        if manifest.is_file():
            try:
                names = frozenset(manifest.read_text(encoding='utf-8').splitlines())
                if names:
                    return cls(names)
            except OSError:
                pass  # fall through to a rebuild

        zip_path = forge_dir / 'res' / 'cardsfolder' / 'cardsfolder.zip'
        names = _parse_cardsfolder(zip_path)
        with contextlib.suppress(OSError):  # a read-only install just rebuilds next time
            manifest.write_text('\n'.join(sorted(names)), encoding='utf-8')
        return cls(names)

    @classmethod
    def from_zip(cls, zip_path: Path) -> ForgeCardIndex:
        """Build an index directly from a ``cardsfolder.zip`` (no manifest cache)."""
        return cls(_parse_cardsfolder(zip_path))


def _parse_cardsfolder(zip_path: Path) -> frozenset[str]:
    """Extract every card's front-face ``Name:`` from a Forge ``cardsfolder.zip``.

    Each ``.txt`` entry is a Forge card script whose first ``Name:`` line is the
    (front-face) card name; that is the authoritative name Forge matches deck
    lines against. Back-face names are covered by :meth:`ForgeCardIndex.has`'s
    front-face normalization, so reading only the first ``Name:`` per file is
    sufficient and fast.
    """
    if not zip_path.is_file():
        raise FileNotFoundError(f'Forge cardsfolder not found: {zip_path}')

    names: set[str] = set()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.endswith('.txt'):
                continue
            with zf.open(info) as fh:
                for raw in fh:
                    line = raw.decode('utf-8', 'replace').strip()
                    if line.startswith('Name:'):
                        names.add(_norm(line[len('Name:') :]))
                        break
    return frozenset(names)
