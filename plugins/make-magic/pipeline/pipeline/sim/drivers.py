"""Per-deck compiled-driver storage + registry (Phase 1: storage + validity only).

A "driver" is a per-deck compiled XMage player (authored + gated in Phase 2) that
plugs into the ``XMageBatch`` ``-Dmakemagic.driverA`` seam. Each deck gets its own
directory under the store data root::

    <data_dir>/drivers/<deck.uuid>/
        Driver.java     # authored source (Phase 2)
        classes/        # compiled .class files, injected onto the run classpath
        meta.json       # provenance + validity stamp (this module)

This module owns ONLY storage + validity: where a deck's driver lives, reading /
writing its ``meta.json``, and deciding whether a compiled driver is still current
for a deck (``driver_valid``). Authoring, compiling, and gating are Phase 2 —
deliberately out of scope here.

Validity is a conjunction of three stamps (see :func:`driver_valid`):

  * ``deck_version`` still equals :func:`pipeline.decks.version.version` — the deck's
    persisted facts have not changed since the driver was built;
  * ``harness_version`` still equals :func:`harness_version` — the XMage jar the
    driver's bytecode was compiled against has not changed its ABI;
  * ``gates_passed`` is ``True`` — Phase 2's gate signed off on this driver.

A missing dir / meta / corrupt json is simply "invalid" (never a crash), so a
caller can always fall back to the driverless path.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline.decks.version import version
from pipeline.sim import xmage_runtime as xr
from pipeline.store.paths import StorePaths

if TYPE_CHECKING:
    from pipeline.contracts import Deck

__all__ = (
    'DRIVERS_DIRNAME',
    'META_FILENAME',
    'DriverMeta',
    'classes_dir',
    'driver_dir',
    'driver_state',
    'driver_valid',
    'harness_version',
    'meta_path',
    'read_meta',
    'write_meta',
)

#: The four states :func:`driver_state` classifies a deck's driver into. ``'valid'`` is the
#: exact :func:`driver_valid` conjunction; the other three split what ``driver_valid`` collapses
#: to a single ``False`` so a router can react differently: ``'stale'`` is RECOVERABLE (an
#: authored driver whose stamp merely lags the current deck/harness — a re-gate can revive it),
#: ``'broken'`` is a gate/parse FAILURE (present but failed sign-off), and ``'absent'`` is simply
#: no driver on disk.
DRIVER_STATES = ('valid', 'stale', 'absent', 'broken')

#: Subdirectory of the store data root that holds per-deck driver dirs.
DRIVERS_DIRNAME = 'drivers'
#: The provenance/validity file inside a deck's driver dir.
META_FILENAME = 'meta.json'
#: The compiled-classes subdir injected (prepended) onto the run classpath.
CLASSES_DIRNAME = 'classes'

#: The core meta.json keys this module owns; any OTHER key read from disk is
#: preserved into :attr:`DriverMeta.extra` so Phase-2 gate stamps survive a
#: read/write round-trip through this module.
_CORE_KEYS = frozenset({'deck_version', 'harness_version', 'fqcn', 'gates_passed', 'gate_mode'})

#: The gate mode a driver was signed off under (Phase 5 dual-mode gate): ``'proactive'``
#: (macro-bearing — gated on macro-fire + never-slower) or ``'reactive'`` (Φ-only — gated on
#: the never-worse-solo floor). The default for a programmatically-built :class:`DriverMeta`
#: without an explicit mode.
_DEFAULT_GATE_MODE = 'proactive'

#: The sentinel a legacy ``meta.json`` predating the ``gate_mode`` field reads back as. The
#: field is descriptive-only (``driver_valid`` does not branch on it), so an absent value must
#: NOT be guessed as ``'proactive'`` — labeling a legacy meta a mode it was never gated under is
#: worse than an honest ``'unknown'``.
_LEGACY_GATE_MODE = 'unknown'


@dataclass(frozen=True)
class DriverMeta:
    """A driver's provenance + validity stamp — the parsed ``meta.json``.

    ``extra`` carries any non-core keys verbatim (Phase 2's gate stamps, e.g. a
    goldfish median or a gate timestamp) so this Phase-1 module never drops fields it
    does not itself understand.
    """

    deck_version: str
    harness_version: str
    fqcn: str
    gates_passed: bool
    gate_mode: str = _DEFAULT_GATE_MODE
    extra: dict[str, object] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        """Serialize to the flat ``meta.json`` dict (core keys + spread ``extra``)."""
        return {
            'deck_version': self.deck_version,
            'harness_version': self.harness_version,
            'fqcn': self.fqcn,
            'gates_passed': self.gates_passed,
            'gate_mode': self.gate_mode,
            **self.extra,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> DriverMeta:
        """Parse a ``meta.json`` dict; unknown keys land in :attr:`extra`.

        Raises ``KeyError`` / ``TypeError`` on a dict missing a core key or with a
        wrong-typed one — callers that must tolerate corruption (:func:`read_meta`)
        catch those.
        """
        extra = {k: v for k, v in data.items() if k not in _CORE_KEYS}
        return cls(
            deck_version=str(data['deck_version']),
            harness_version=str(data['harness_version']),
            fqcn=str(data['fqcn']),
            gates_passed=bool(data['gates_passed']),
            gate_mode=str(data.get('gate_mode', _LEGACY_GATE_MODE)),
            extra=extra,
        )


def _data_root(data_dir: str | os.PathLike[str] | None) -> Path:
    """The store data root — the same resolution the rest of ``sim/`` uses."""
    return Path(data_dir) if data_dir is not None else StorePaths.resolve().data_dir


def driver_dir(deck: Deck, *, data_dir: str | os.PathLike[str] | None = None) -> Path:
    """``<data_dir>/drivers/<deck.uuid>`` — a deck's driver dir (keyed by identity).

    Keyed on ``deck.uuid`` (the stable, name-independent deck identity), NOT the name
    or the content version, so a driver stays bound to the deck across renames + edits
    (its staleness is then judged by :func:`driver_valid`, not its location)."""
    return _data_root(data_dir) / DRIVERS_DIRNAME / deck.uuid


def classes_dir(deck: Deck, *, data_dir: str | os.PathLike[str] | None = None) -> Path:
    """``<driver_dir>/classes`` — the compiled-classes dir prepended onto the classpath."""
    return driver_dir(deck, data_dir=data_dir) / CLASSES_DIRNAME


def meta_path(deck: Deck, *, data_dir: str | os.PathLike[str] | None = None) -> Path:
    """``<driver_dir>/meta.json`` — a deck's driver provenance/validity file."""
    return driver_dir(deck, data_dir=data_dir) / META_FILENAME


def read_meta(deck: Deck, *, data_dir: str | os.PathLike[str] | None = None) -> DriverMeta | None:
    """Read + parse a deck's ``meta.json``, or ``None`` if missing/corrupt/incomplete.

    Never raises: a missing dir, a missing file, malformed JSON, or a dict missing a
    core key all resolve to ``None`` so a caller can treat "no valid driver" uniformly.
    """
    path = meta_path(deck, data_dir=data_dir)
    try:
        raw = path.read_text(encoding='utf-8')
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return DriverMeta.from_json(data)
    except (KeyError, TypeError, ValueError):
        return None


def write_meta(deck: Deck, meta: DriverMeta, *, data_dir: str | os.PathLike[str] | None = None) -> None:
    """Write a deck's ``meta.json`` (creating the driver dir on demand), atomically.

    Serializes ``meta`` (core keys + spread ``extra``) and publishes via a temp-file +
    ``os.replace`` so a crash mid-write never leaves a half-written meta that
    :func:`read_meta` would reject and a caller would misread as "no driver".
    """
    ddir = driver_dir(deck, data_dir=data_dir)
    ddir.mkdir(parents=True, exist_ok=True)
    dest = ddir / META_FILENAME
    tmp = ddir / f'{META_FILENAME}.tmp'
    tmp.write_text(json.dumps(meta.to_json(), indent=2, sort_keys=True), encoding='utf-8')
    os.replace(tmp, dest)


def harness_version(*, data_dir: str | os.PathLike[str] | None = None) -> str:
    """The identity of the XMage jar a compiled driver's bytecode is bound to.

    A driver is compiled against the shipped XMage jar's classes; if that jar changes
    its ABI the compiled ``.class`` files can break, so the jar identity is a validity
    input alongside the deck version.

    * When :data:`pipeline.sim.xmage_runtime.XMAGE_DIST_SHA256` is PINNED (a cut
      release) that pinned SHA *is* the identity — no jar read needed.
    * In LOCAL-DEV (pin is ``None``) there is no published SHA, so fall back to hashing
      the resolved jar file itself (the first classpath entry: the harness jar in
      reactor mode, the dist jar in fetched-jar mode) so staleness still tracks the
      actual built bytes.
    """
    pinned = xr.XMAGE_DIST_SHA256
    if pinned is not None:
        return pinned
    install = xr.resolve(data_dir=data_dir)
    jar = Path(install.classpath.split(os.pathsep)[0])
    return hashlib.sha256(jar.read_bytes()).hexdigest()


def driver_valid(deck: Deck, *, data_dir: str | os.PathLike[str] | None = None) -> bool:
    """True iff ``deck`` has a CURRENT, gate-passed compiled driver on disk.

    Conjunction of: meta present + parseable, ``gates_passed`` true, the stamped
    ``deck_version`` equals the deck's current :func:`version`, and the stamped
    ``harness_version`` equals the current :func:`harness_version`. Any miss (incl. a
    missing dir/meta) → ``False`` — the caller falls back to the driverless path.
    """
    return driver_state(deck, data_dir=data_dir) == 'valid'


def driver_state(deck: Deck, *, data_dir: str | os.PathLike[str] | None = None) -> str:
    """Classify ``deck``'s driver into one of :data:`DRIVER_STATES` — the STALE/ABSENT/BROKEN
    split :func:`driver_valid` collapses into a single ``False``.

    * ``'absent'`` — no ``meta.json`` on disk at all (a fresh deck / never-authored driver).
    * ``'broken'`` — a ``meta.json`` IS on disk but it is unparseable/corrupt, OR it parses with
      ``gates_passed`` false (the gate ran and REJECTED this driver). A re-gate is warranted only
      after a recompile; the stamp itself says "do not trust".
    * ``'stale'`` — parses, ``gates_passed`` true, but the stamped ``deck_version`` OR
      ``harness_version`` no longer matches the current values (the deck was edited, or — the jar
      cut — the harness ABI moved). RECOVERABLE by re-gating against the current harness.
    * ``'valid'`` — the full :func:`driver_valid` conjunction holds.

    Distinguishing ABSENT from BROKEN needs the raw file (``read_meta`` maps both to ``None``): a
    missing file is absent; a present-but-``None`` file is broken.
    """
    path = meta_path(deck, data_dir=data_dir)
    if not path.is_file():
        return 'absent'
    meta = read_meta(deck, data_dir=data_dir)
    if meta is None:
        return 'broken'  # file present but corrupt/incomplete — a failed/garbled stamp.
    if not meta.gates_passed:
        return 'broken'  # the gate ran and did NOT sign off.
    if meta.deck_version != version(deck) or meta.harness_version != harness_version(data_dir=data_dir):
        return 'stale'  # signed off, but the deck or the harness moved under it — re-gate can revive.
    return 'valid'
