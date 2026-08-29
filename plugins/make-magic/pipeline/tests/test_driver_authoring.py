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
from pipeline.transforms.combo_detect import Combo

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


def test_mulligan_note_appears_in_javadoc() -> None:
    """The primer Mulligan guidance is carried as prose in the class javadoc (independent of
    whether the live slot is wired)."""
    src = da.render_quad_driver(_deck(), da.JELEVA_QUAD_SPEC)
    assert 'Mulligan' in src
    assert da.JELEVA_QUAD_SPEC.mulligan_note in src


def test_proactive_quad_with_mulligan_wires_mulligan_registry() -> None:
    """A spec carrying a MulliganSpec (the restored 5th dimension) emits a MulliganSteer inner
    class + a MulliganRegistry.register call, so the dist's chooseMulligan hook consults it."""
    src = da.render_quad_driver(_deck(), da.JELEVA_QUAD_SPEC)
    assert da.JELEVA_QUAD_SPEC.mulligan is not None
    assert 'import mage.player.ai.score.MulliganRegistry;' in src
    assert 'import mage.player.ai.score.MulliganSteer;' in src
    assert 'MulliganRegistry.register(playerId, new Mull());' in src
    assert 'private static final class Mull implements MulliganSteer {' in src
    assert 'public boolean shipHand(Game game, UUID pid) {' in src
    # the wired-registry breadcrumb names the mulligan slot
    assert 'mull=MulliganRegistry' in src


def test_quad_without_mulligan_omits_mulligan_registry() -> None:
    """A spec with mulligan=None registers no mulligan — the seat keeps CP7's default heuristic
    (the emitter emits neither the MulliganSteer inner class nor its import/registration)."""
    src = da.render_quad_driver(_deck(), da.SHORIKAI_REACTIVE_QUAD_SPEC)
    assert da.SHORIKAI_REACTIVE_QUAD_SPEC.mulligan is None
    assert 'MulliganRegistry' not in src
    assert 'implements MulliganSteer' not in src
    assert 'import mage.player.ai.score.MulliganSteer;' not in src


def test_phi_only_reactive_plus_mulligan_wires_only_phi_and_mulligan() -> None:
    """A Φ-only reactive quad that ALSO owns mulligan registers DriverBonus + MulliganRegistry
    only — no macro/P/S — proving the mulligan slot composes independently of the macro."""
    spec = da.QuadSpec(
        name='reactive-plus-mulligan',
        archetype='reactive',
        phi_body=da.SHORIKAI_REACTIVE_QUAD_SPEC.phi_body,
        imports=da.SHORIKAI_REACTIVE_QUAD_SPEC.imports,
        mulligan=da.MulliganSpec(ship_body='return me.getHand().size() < 6;'),
    )
    src = da.render_quad_driver(_deck(), spec)
    assert 'DriverBonus.register(playerId, Driver::phi);' in src
    assert 'MulliganRegistry.register(playerId, new Mull());' in src
    assert 'implements MulliganSteer' in src
    # still no macro / S
    assert 'MacroRegistry' not in src
    assert 'SelectionRegistry' not in src
    assert 'implements ComboMacro' not in src


# --------------------------------------------------------------------------- #
# Combo-litmus additions (Phase 1): QuadSpec.thin / archetypes / seeding       #
# --------------------------------------------------------------------------- #


def test_quadspec_thin_is_neutral_bare_cp7() -> None:
    """A thin QuadSpec renders a NEUTRAL driver: Φ=0, no macro/steer/mulligan classes,
    and it passes the guardrails (it is behaviorally bare CP7 — registers a no-op Φ)."""
    spec = da.QuadSpec.thin('some-value-deck')
    assert spec.archetype == 'thin'
    assert spec.macro is None
    assert spec.steer is None
    assert spec.mulligan is None
    src = da.render_quad_driver(_deck(), spec)
    assert 'private static int phi(Game game, UUID pid) {' in src
    assert 'return 0;' in src
    assert 'implements ComboMacro' not in src
    assert 'implements SelectionSteer' not in src
    assert 'implements MulliganSteer' not in src
    da.check_quad_guardrails(src)  # must not raise


def test_archetype_vocabulary_and_validation() -> None:
    """The archetype vocabulary is the three combo-litmus values; an invalid one is rejected."""
    assert da.ARCHETYPES == ('drive-dedicated', 'drive-capable', 'thin')
    da.validate_archetype('drive-dedicated')
    da.validate_archetype('drive-capable')
    da.validate_archetype('thin')
    with pytest.raises(ValueError):
        da.validate_archetype('proactive')
    with pytest.raises(ValueError):
        da.QuadSpec.thin('x', archetype='reactive')


def _win_combo() -> Combo:
    return Combo(
        variant_id='synthetic-1',
        card_names=("Thassa's Oracle", 'Demonic Consultation'),
        card_oracle_ids=('oid-a', 'oid-b'),
        result='Each opponent loses the game',
    )


def test_seed_quad_from_combo_produces_drive_quad() -> None:
    """Rule-3 seeding fills the fixed template from the Combo: P checks all piece names,
    a Macro + Steer are emitted, and the archetype flows from the arg."""
    spec = da.seed_quad_from_combo(_win_combo(), archetype='drive-capable')
    assert spec.archetype == 'drive-capable'
    assert spec.macro is not None
    assert spec.steer is not None
    src = da.render_quad_driver(_deck(), spec)
    # every piece name is concretely generated into P (and S)
    assert "Thassa's Oracle" in src
    assert 'Demonic Consultation' in src
    assert 'private static final class Macro implements ComboMacro {' in src
    assert 'private static final class Steer implements SelectionSteer {' in src
    da.check_quad_guardrails(src)


def test_seed_quad_dedicated_stages_pieces_in_phi() -> None:
    """A dedicated seed carries a piece-staging Φ; a capable seed is thin Φ=0."""
    capable = da.seed_quad_from_combo(_win_combo(), archetype='drive-capable')
    dedicated = da.seed_quad_from_combo(_win_combo(), archetype='drive-dedicated')
    assert 'return 0;' in capable.phi_body
    assert 'pieces' in dedicated.phi_body


def test_seed_quad_rejects_bad_archetype() -> None:
    with pytest.raises(ValueError):
        da.seed_quad_from_combo(_win_combo(), archetype='thin')


# --------------------------------------------------------------------------- #
# Opportunistic nudge Φ — the missing middle of the magnitude axis            #
# --------------------------------------------------------------------------- #


def test_nudge_alpha_zero_is_identical_to_thin_phi() -> None:
    """alpha=0 must be byte-identical to thin Φ (return 0;) — the axis origin does nothing."""
    body = da._nudge_phi_body(_win_combo().card_names, alpha=0)
    assert body == 'return 0;'


def test_nudge_phi_has_cheap_early_out_and_bounded_alpha() -> None:
    """alpha>0 Φ carries the cheap early-out gate (return 0 when no piece is live), scans hand AND
    battlefield for piece presence, and scales its score by the alpha literal — bounded well under 1e6."""
    alpha = 2000
    body = da._nudge_phi_body(_win_combo().card_names, alpha=alpha)
    # early-out gate present (return 0 before any assembly scoring)
    assert 'anyLive' in body
    assert 'return 0;' in body
    # piece detection reuses the dedicated presence logic (hand + battlefield)
    assert 'me.getHand().getCards(game)' in body
    assert 'game.getBattlefield().getAllActivePermanents(pid)' in body
    # alpha is the per-piece weight, a SMALL bounded nudge (not the dedicated 40000 dominant term)
    assert f'* {alpha}' in body
    # the assembly bonus is alpha-scaled and the whole Φ stays bounded well under ±1e6
    assert f'{alpha * da.NUDGE_ASSEMBLY_BONUS_MULT}' in body
    max_score = len(_win_combo().card_names) * alpha + alpha * da.NUDGE_ASSEMBLY_BONUS_MULT
    assert max_score < 1_000_000


def test_nudge_phi_magnitude_is_monotonic_in_alpha() -> None:
    """A larger alpha yields strictly larger nudge literals — the magnitude knob is monotone."""
    small = da._nudge_phi_body(_win_combo().card_names, alpha=2000)
    large = da._nudge_phi_body(_win_combo().card_names, alpha=40000)
    assert '* 2000' in small
    assert '* 40000' in large


def test_seed_nudge_quad_carries_macro_steer_and_nudge_phi() -> None:
    """The opportunistic quad keeps the rule-3 macro + steer (fires when pieces assemble) and a
    small-alpha nudge Φ that does NOT force assembly. alpha=0 collapses the Φ to thin."""
    thin = da.seed_nudge_quad(_win_combo(), alpha=0)
    assert thin.macro is not None  # opportunistic macro present even at alpha=0
    assert thin.steer is not None
    assert 'return 0;' in thin.phi_body
    assert 'anyLive' not in thin.phi_body

    opp = da.seed_nudge_quad(_win_combo(), alpha=2000)
    assert opp.macro is not None
    assert 'anyLive' in opp.phi_body
    src = da.render_quad_driver(_deck(), opp)
    assert "Thassa's Oracle" in src
    assert 'private static final class Macro implements ComboMacro {' in src
    da.check_quad_guardrails(src)  # must not raise


def test_seed_nudge_quad_rejects_negative_alpha() -> None:
    with pytest.raises(ValueError):
        da.seed_nudge_quad(_win_combo(), alpha=-1)


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
    """Fetch the pinned REAL ECJ jar into the tools cache, or skip if it can't be reached."""
    try:
        dc.ensure_ecj(data_dir=data_dir)
    except dc.DriverCompileToolError as exc:
        pytest.skip(f'pinned ECJ jar unreachable (offline?): {exc}')


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
    # the restored 5th dimension: the mulligan inner class compiles against MulliganSteer
    assert (pkg / f'{da.DRIVER_SIMPLE_CLASS}$Mull.class').is_file()


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


@pytest.mark.integration
def test_thin_quad_ecj_compiles_against_real_dist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A THIN quad (Φ=0, no macro/steer/mull) ECJ-compiles against the real dist — bare CP7."""
    result, pkg_rel = _compile_quad(da.QuadSpec.thin('thin-value-deck'), monkeypatch, tmp_path)
    assert result.ok, result.raw_stderr
    assert result.class_dir is not None
    pkg = result.class_dir / pkg_rel
    assert (pkg / f'{da.DRIVER_SIMPLE_CLASS}.class').is_file()
    assert not (pkg / f'{da.DRIVER_SIMPLE_CLASS}$Macro.class').is_file()
    assert not (pkg / f'{da.DRIVER_SIMPLE_CLASS}$Steer.class').is_file()


@pytest.mark.integration
def test_combo_seeded_quad_ecj_compiles_against_real_dist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A quad SEEDED from a Combo (rule-3 template) ECJ-compiles against the real dist for both
    Φ-modes (the P precondition + S fetch are concretely generated from card_names)."""
    combo = Combo(
        variant_id='synthetic-1',
        card_names=("Thassa's Oracle", 'Demonic Consultation'),
        card_oracle_ids=('oid-a', 'oid-b'),
        result='Each opponent loses the game',
    )
    for arch in ('drive-capable', 'drive-dedicated'):
        spec = da.seed_quad_from_combo(combo, archetype=arch)
        result, pkg_rel = _compile_quad(spec, monkeypatch, tmp_path / arch)
        assert result.ok, result.raw_stderr
        assert result.class_dir is not None
        pkg = result.class_dir / pkg_rel
        assert (pkg / f'{da.DRIVER_SIMPLE_CLASS}$Macro.class').is_file()
        assert (pkg / f'{da.DRIVER_SIMPLE_CLASS}$Steer.class').is_file()


@pytest.mark.integration
def test_nudge_quad_ecj_compiles_across_alpha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The opportunistic-nudge quad ECJ-compiles against the real dist at every alpha on the sweep
    axis (0=thin, small, medium, large≈dedicated) — the early-out gate + alpha-scaled Φ are valid Java."""
    combo = Combo(
        variant_id='synthetic-1',
        card_names=("Thassa's Oracle", 'Demonic Consultation'),
        card_oracle_ids=('oid-a', 'oid-b'),
        result='Each opponent loses the game',
    )
    for alpha in (0, 2000, 12000, 40000):
        spec = da.seed_nudge_quad(combo, alpha=alpha)
        result, pkg_rel = _compile_quad(spec, monkeypatch, tmp_path / f'a{alpha}')
        assert result.ok, result.raw_stderr
        assert result.class_dir is not None
        pkg = result.class_dir / pkg_rel
        assert (pkg / f'{da.DRIVER_SIMPLE_CLASS}.class').is_file()
        assert (pkg / f'{da.DRIVER_SIMPLE_CLASS}$Macro.class').is_file()
