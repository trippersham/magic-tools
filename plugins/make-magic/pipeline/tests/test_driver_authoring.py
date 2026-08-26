"""Phase 4 authoring tests — the QUAD emitter (primer Strategy → register-by-playerId Java).

The unit tests are pure string generation: NO JVM. They assert the emitted class is of the
reference-driver shape (a ``static register(UUID)`` wiring the three seam registries), that a
proactive spec wires all three slots, and that a Φ-only reactive spec wires ONLY Φ (no macro,
no S inner class). The ``@pytest.mark.integration`` tests ECJ-compile BOTH a proactive and a
Φ-only rendered quad against the local dist jar — deselected by default so CI without the
toolchain still collects (opt in with ``-m integration`` + the env in the Phase 4 spec).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline.contracts.models import Deck, DeckCard
from pipeline.sim import driver_authoring as da
from pipeline.sim import driver_compile as dc

_REAL_ECJ = os.environ.get('MAKE_MAGIC_ECJ_JAR')
_LOCAL_DIST = (
    Path(__file__).resolve().parents[1]
    / 'pipeline'
    / 'sim'
    / 'java'
    / 'xmage-dist'
    / 'target'
    / 'make-magic-xmage-dist.jar'
)


def _deck(name: str = 'Test Deck') -> Deck:
    return Deck(name=name, cards=[DeckCard(name='Forest', quantity=1)])


# --------------------------------------------------------------------------- #
# FQCN / package                                                              #
# --------------------------------------------------------------------------- #


def test_fqcn_and_package_are_legal_java_identifiers() -> None:
    """A 32-hex uuid can begin with a digit — the package leaf must be prefixed so the FQCN is
    a legal Java name the seam can reflectively load + ``register`` on."""
    deck = _deck()
    pkg = da.driver_package(deck)
    fqcn = da.driver_fqcn(deck)
    assert pkg == f'{da.DRIVER_PACKAGE_ROOT}.d_{deck.uuid}'
    assert fqcn == f'{pkg}.Driver'
    for seg in fqcn.split('.'):
        assert seg[0].isalpha() or seg[0] == '_'
        assert all(c.isalnum() or c == '_' for c in seg)


# --------------------------------------------------------------------------- #
# The quad scaffold (shape of the emitted class)                              #
# --------------------------------------------------------------------------- #


def test_proactive_quad_wires_all_three_registries() -> None:
    """A proactive spec (macro + steer) renders the reference-driver shape: a static
    ``register(UUID)`` wiring Φ, macro, and S into the three ``playerId``-keyed registries,
    plus the Φ method and both inner classes."""
    deck = _deck()
    src = da.render_quad_driver(deck, da.JELEVA_QUAD_SPEC)
    assert f'package {da.driver_package(deck)};' in src
    assert 'public final class Driver {' in src
    assert 'public static void register(UUID playerId) {' in src
    # the three registry calls, keyed by playerId
    assert 'DriverBonus.register(playerId, Driver::phi);' in src
    assert 'MacroRegistry.register(playerId, new Macro());' in src
    assert 'SelectionRegistry.register(playerId, new Steer());' in src
    # the slot implementations
    assert 'private static int phi(Game game, UUID pid) {' in src
    assert 'private static final class Macro implements ComboMacro {' in src
    assert 'public boolean applicable(Game game, UUID pid) {' in src
    assert 'public void apply(Game game, UUID pid) {' in src
    assert 'private static final class Steer implements SelectionSteer {' in src
    # the observability marker
    assert f'{da.DRIVER_REGISTERED_MARKER} playerId=' in src


def test_phi_only_reactive_quad_omits_macro_and_steer() -> None:
    """A Φ-only reactive spec (macro is None, steer is None) registers ONLY Φ and emits NO
    macro/P/S — neither the MacroRegistry/SelectionRegistry calls nor the inner classes."""
    src = da.render_quad_driver(_deck(), da.SHORIKAI_REACTIVE_QUAD_SPEC)
    assert 'DriverBonus.register(playerId, Driver::phi);' in src
    assert 'private static int phi(Game game, UUID pid) {' in src
    # NO macro / P / S for a Φ-only reactive deck
    assert 'MacroRegistry' not in src
    assert 'SelectionRegistry' not in src
    assert 'implements ComboMacro' not in src
    assert 'implements SelectionSteer' not in src
    # and it does not import the registry types it never touches
    assert 'import mage.player.ai.score.MacroRegistry;' not in src
    assert 'import mage.player.ai.score.SelectionRegistry;' not in src


def test_quad_is_not_a_computerplayer7_subclass() -> None:
    """The quad registers by playerId — it is NOT an engine-replacement subclass. No
    ComputerPlayer7 extension and no ``copy()`` to override (design §7.1 / §8 retire)."""
    src = da.render_quad_driver(_deck(), da.JELEVA_QUAD_SPEC)
    assert 'extends ComputerPlayer7' not in src
    assert 'copy()' not in src


def test_phi_scaffold_injects_null_guarded_me() -> None:
    """The Φ scaffold prepends the non-null ``Player me`` guard so a slot body may use ``me``
    without re-deriving it (and cannot NPE on a torn-down player)."""
    src = da.render_quad_driver(_deck(), da.SHORIKAI_REACTIVE_QUAD_SPEC)
    assert 'Player me = game.getPlayer(pid);' in src
    assert 'if (me == null) {' in src


def test_mulligan_note_is_documented_not_registered() -> None:
    """The primer Mulligan guidance is carried as documentation (the frozen seam has no
    mulligan registry) — it appears in the class javadoc, never as a registry call."""
    src = da.render_quad_driver(_deck(), da.JELEVA_QUAD_SPEC)
    assert 'Mulligan' in src
    assert da.JELEVA_QUAD_SPEC.mulligan_note in src


# --------------------------------------------------------------------------- #
# ECJ compile — the load-bearing acceptance bar (proactive AND Φ-only)         #
# --------------------------------------------------------------------------- #


def _runnable_java() -> str | None:
    java = os.environ.get('MAKE_MAGIC_JAVA') or shutil.which('java')
    if not java:
        return None
    try:
        subprocess.run([java, '-version'], capture_output=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return java


def _stage_real_ecj(data_dir: Path) -> None:
    if not _REAL_ECJ or not Path(_REAL_ECJ).is_file():
        pytest.skip('no real ECJ jar (set MAKE_MAGIC_ECJ_JAR to a pinned ecj-*.jar)')
    tools = data_dir / 'xmage' / 'tools'
    tools.mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(_REAL_ECJ), tools / dc._ECJ_JAR_NAME)


def _compile_quad(
    spec: da.QuadSpec, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[dc.CompileResult, Path]:
    """Render ``spec`` into the deck's package tree + ECJ-compile it against the local dist."""
    java = _runnable_java()
    if java is None:
        pytest.skip('no runnable java (set MAKE_MAGIC_JAVA)')
    if not _LOCAL_DIST.is_file():
        pytest.skip(f'no local dist jar at {_LOCAL_DIST}')
    monkeypatch.setenv('MAKE_MAGIC_JAVA', java)
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    _stage_real_ecj(tmp_path)
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_DIST_JAR', str(_LOCAL_DIST))

    deck = _deck()
    src_text = da.render_quad_driver(deck, spec)
    pkg_dir = tmp_path / 'src' / Path(*da.driver_package(deck).split('.'))
    pkg_dir.mkdir(parents=True)
    src = pkg_dir / f'{da.DRIVER_SIMPLE_CLASS}.java'
    src.write_text(src_text, encoding='utf-8')
    pkg_rel = Path(*da.driver_package(deck).split('.'))
    return dc.compile_driver(src, data_dir=tmp_path), pkg_rel


@pytest.mark.integration
def test_proactive_quad_ecj_compiles_against_real_dist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The PROACTIVE Jeleva quad ECJ-compiles against the REAL local dist jar (all three
    registry types + the ComboMacro/SelectionSteer inner classes resolve)."""
    result, pkg_rel = _compile_quad(da.JELEVA_QUAD_SPEC, monkeypatch, tmp_path)
    assert result.ok, result.raw_stderr
    assert result.class_dir is not None
    pkg = result.class_dir / pkg_rel
    assert (pkg / f'{da.DRIVER_SIMPLE_CLASS}.class').is_file()
    assert (pkg / f'{da.DRIVER_SIMPLE_CLASS}$Macro.class').is_file()
    assert (pkg / f'{da.DRIVER_SIMPLE_CLASS}$Steer.class').is_file()


@pytest.mark.integration
def test_phi_only_reactive_quad_ecj_compiles_against_real_dist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Φ-ONLY reactive quad ECJ-compiles against the REAL local dist jar with ONLY the Φ
    method (no macro/S inner classes emitted)."""
    result, pkg_rel = _compile_quad(da.SHORIKAI_REACTIVE_QUAD_SPEC, monkeypatch, tmp_path)
    assert result.ok, result.raw_stderr
    assert result.class_dir is not None
    pkg = result.class_dir / pkg_rel
    assert (pkg / f'{da.DRIVER_SIMPLE_CLASS}.class').is_file()
    assert not (pkg / f'{da.DRIVER_SIMPLE_CLASS}$Macro.class').is_file()
    assert not (pkg / f'{da.DRIVER_SIMPLE_CLASS}$Steer.class').is_file()
