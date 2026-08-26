"""Phase 4 — the per-deck **quad** driver AUTHORING surface (primer Strategy → Java).

Phase 3 productionized the in-search seam as **register-by-playerId**: PlayerA stays a plain
``ComputerPlayer7`` and a Driver *registers* a quad ``(Φ, P, macro, S)`` by ``playerId`` into
three public dist registries (``DriverBonus`` / ``MacroRegistry`` / ``SelectionRegistry``),
which the patched minimax consults inside the search. This module emits a Java class of that
shape — the reference implementation is
``pipeline/pipeline/sim/reference_drivers/JelevaThoracleReferenceDriver.java``.

The authoring pipeline (``skills/authoring-drivers``) derives the quad from a deck's
**deck-primer Strategy** (``strategy-schema.md``): Gameplan → Φ, Win Condition → macro,
Assembly → P (the macro's ``applicable``), Sequencing → S. A **Φ-only reactive** deck emits Φ
(+ optional helpers) and NO macro/P/S. This module owns ONE thing: turning that primer-derived
:class:`QuadSpec` into a compilable ``Driver.java`` of the reference-driver shape — a
``public static void register(UUID)`` wiring the (up to) three registries around the author's
per-slot bodies, so a fill can never get the ingestion contract wrong.

The load-bearing discipline the scaffold guarantees (so a fill cannot forget it — see design
§7.1):

  * **Own the noun, defer the verb.** The real-seat slots (S, and the macro's real fire) act
    ONCE on an owned decision and defer everything else; the scaffold never emits combat /
    land-drop / attacker ownership. Φ and the in-search macro-fold run inside the search by
    design (they cannot regress the real seat).
  * **Category/mechanic altitude.** A slot body matches by comprehensive predicate over a
    category (``getName()`` against the FULL member set of a category is fine when knowable at
    design time); never a single card as a proxy for a category.
  * **No ``priority()``/``copy()`` on the handed sim.** The macro's ``apply`` drives the known
    outcome with bounded explicit state moves; it never calls ``priority()`` or ``copy()`` on
    the game it is handed. There is no ``ComputerPlayer7`` subclass, so there is no ``copy()``
    to override.

**Retired:** the external ``-Dmakemagic.driverA`` ``ComputerPlayer7``-subclass template and the
``probeWins`` / greedy-assembly go/no-go skeleton — the go/no-go is emergent in-search now (the
macro-fold + Φ decide whether firing wins). The thin DEPRECATED compat shim
(``_DRIVER_TEMPLATE`` / ``render_driver`` / ``MIKAEUS_LINE_SPEC``) that survived P4 was DELETED
in Phase 5 once ``driver_gate`` rewired to the quad surface; all authoring goes through
:func:`render_quad_driver`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.contracts import Deck

__all__ = (
    # Names sorted (ruff RUF022).
    'DRIVER_MACRO_FIRED_MARKER',
    'DRIVER_PACKAGE_ROOT',
    'DRIVER_REGISTERED_MARKER',
    'DRIVER_SIMPLE_CLASS',
    'DRIVER_STEER_FIRED_MARKER',
    'JELEVA_QUAD_SPEC',
    'SHORIKAI_REACTIVE_QUAD_SPEC',
    'GuardrailViolation',
    'MacroSpec',
    'QuadSpec',
    'SteerSpec',
    'check_quad_guardrails',
    'driver_fqcn',
    'driver_package',
    'render_quad_driver',
)

#: The package prefix every generated driver lives under (per-deck leaf appended).
DRIVER_PACKAGE_ROOT = 'makemagic.driver'
#: The (fixed) simple class name; deck identity lives in the package leaf so every driver's
#: source is literally ``Driver.java`` (the file the harness writes + compiles).
DRIVER_SIMPLE_CLASS = 'Driver'
#: The stderr marker the emitted ``register(UUID)`` prints once its quad is wired — an
#: observability breadcrumb alongside XMageBatch's own ``DRIVER_REGISTERED`` line.
DRIVER_REGISTERED_MARKER = 'QUAD_DRIVER_REGISTERED'
#: STANDARD slot-exercise markers (Phase 5) the EMITTER injects into every authored quad — so
#: the dual-mode gate can assert slot exercise DECK-AGNOSTICALLY (not off a deck-specific
#: println). :data:`DRIVER_MACRO_FIRED_MARKER` is printed at the top of the macro's ``apply``
#: (which the seam invokes ONLY to execute the deterministic win ⇒ entry == the macro fired);
#: :data:`DRIVER_STEER_FIRED_MARKER` is printed when the steer actually added a target (a true
#: return), never when it defers to CP7. Deck-specific logs (e.g. ``MACRO_ORACLE_ETB_WIN``) are
#: kept alongside for debugging.
DRIVER_MACRO_FIRED_MARKER = 'DRIVER_MACRO_FIRED'
DRIVER_STEER_FIRED_MARKER = 'DRIVER_STEER_FIRED'


def _sanitize(uuid: str) -> str:
    """A deck uuid → a legal Java package-segment leaf (``d_<hex>``).

    The store's ``deck.uuid`` is a 32-char hex string, which cannot begin a Java identifier if
    it starts with a digit — so prefix ``d_`` and replace any stray non-word char, keeping the
    driver's FQCN stable + legal across decks.
    """
    return 'd_' + re.sub(r'\W', '_', uuid)


def driver_package(deck: Deck) -> str:
    """``makemagic.driver.d_<uuid>`` — the per-deck package the driver is emitted into."""
    return f'{DRIVER_PACKAGE_ROOT}.{_sanitize(deck.uuid)}'


def driver_fqcn(deck: Deck) -> str:
    """``makemagic.driver.d_<uuid>.Driver`` — the FQCN the seam loads via reflection.

    Passed to ``-Dmakemagic.driver`` and stamped into the driver's ``meta.json`` so the run
    path and the gate agree on exactly which class ``XMageBatch`` reflectively ``register``s.
    """
    return f'{driver_package(deck)}.{DRIVER_SIMPLE_CLASS}'


# --------------------------------------------------------------------------- #
# The quad input model — a primer-derived spec (typed), rendered to Java.       #
#                                                                               #
# Each slot carries the author's Java *body* (statements); the scaffold owns    #
# the ingestion contract (register(), the method/inner-class signatures, the    #
# me-null guard) so a fill only writes deck logic, never the wiring.            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MacroSpec:
    """The macro slot: the deterministic win sequence + its precondition P.

    ``applicable_body`` (P, from the primer **Assembly** section) is the body of
    ``boolean applicable(Game game, UUID pid)`` — return whether the kill is executable NOW.
    ``apply_body`` (the macro, from **Win Condition**) is the body of
    ``void apply(Game game, UUID pid)`` — drive the known outcome with BOUNDED explicit state
    moves (``moveCards`` / ``applyEffects`` / a capped ``getStack().resolve``); NEVER call
    ``priority()`` or ``copy()`` on the handed game. Both bodies see ``game`` and ``pid``.
    """

    applicable_body: str
    apply_body: str


@dataclass(frozen=True)
class SteerSpec:
    """The S slot: a selection steer over a library/hand card search (from **Sequencing**).

    ``apply_body`` is the body of
    ``boolean apply(Game game, UUID pid, Cards cards, TargetCard target, Ability source,
    boolean useAddTarget)`` — steer MY OWN search (guard on ``source.getControllerId()``) to a
    category-comprehensive pick, ``return true`` when it added a target, else ``return false``
    to defer to CP7. Match by a comprehensive predicate over a category, never one card as a
    proxy (design §7.1 category-altitude).
    """

    apply_body: str


@dataclass(frozen=True)
class QuadSpec:
    """The primer-derived quad — the typed input to :func:`render_quad_driver`.

    ``phi_body`` (Φ, from **Gameplan**) is the body of ``int phi(Game game, UUID pid)`` — a
    monotone, bounded (well within ±1e6) potential toward the gameplan; the scaffold prepends a
    non-null ``Player me`` guard so the body may use ``me``. ``macro`` is present for a
    **proactive** deck (Win Condition + Assembly) and ``None`` for a **Φ-only reactive** deck
    (no macro/P registered). ``steer`` is the optional S slot. ``helpers`` are class-level
    ``private static`` helper methods the slot bodies share (e.g. an ``untappedLands`` mana
    proxy). ``imports`` are the extra ``mage.*`` imports the bodies reference (beyond the
    always-present ``Game`` / ``Player`` / ``UUID`` + the registry types the scaffold wires).
    ``mulligan_note`` records the primer **Mulligan** guidance as documentation only — the
    frozen seam has no mulligan registry to bind it to (a P6.6 follow-up).
    """

    name: str
    #: 'proactive' or 'reactive' — the primer archetype call that routes gate mode. Purely
    #: documentary in the emitter; the *presence of a macro* is the machine signal.
    archetype: str
    phi_body: str
    macro: MacroSpec | None = None
    steer: SteerSpec | None = None
    helpers: str = ''
    imports: tuple[str, ...] = ()
    mulligan_note: str = ''


def _indent(body: str, spaces: int) -> str:
    """Re-indent a (possibly multi-line) Java body to ``spaces`` columns, blanks preserved."""
    pad = ' ' * spaces
    return '\n'.join(pad + line if line.strip() else '' for line in body.splitlines())


def render_quad_driver(deck: Deck, spec: QuadSpec) -> str:
    """Render ``spec`` into a compilable quad ``Driver.java`` of the reference-driver shape.

    Emits ``public final class Driver`` in :func:`driver_package` with a
    ``public static void register(UUID playerId)`` that wires Φ into ``DriverBonus``, and —
    when present — the macro into ``MacroRegistry`` and the steer into ``SelectionRegistry``.
    A **Φ-only reactive** ``spec`` (``macro is None`` and ``steer is None``) registers ONLY Φ
    and emits neither the ``Macro`` nor the ``Steer`` inner class. The registries an
    unregistered seat never touches stay pure CP7 (the intelligence-preserving property).
    """
    pkg = driver_package(deck)
    has_macro = spec.macro is not None
    has_steer = spec.steer is not None

    imports = [
        'import java.util.UUID;',
        '',
        'import mage.game.Game;',
        'import mage.players.Player;',
        'import mage.player.ai.score.DriverBonus;',
    ]
    if has_macro:
        imports += [
            'import mage.player.ai.score.ComboMacro;',
            'import mage.player.ai.score.MacroRegistry;',
        ]
    if has_steer:
        imports += [
            'import mage.abilities.Ability;',
            'import mage.cards.Cards;',
            'import mage.player.ai.score.SelectionRegistry;',
            'import mage.player.ai.score.SelectionSteer;',
            'import mage.target.TargetCard;',
        ]
    if spec.imports:
        imports += ['', *spec.imports]

    # ---- register(UUID): wire the quad by playerId (up to three registries) ----
    reg_lines = ['        DriverBonus.register(playerId, Driver::phi);']
    wired = ['Phi=DriverBonus']
    if has_macro:
        reg_lines.append('        MacroRegistry.register(playerId, new Macro());')
        wired.append('macro=MacroRegistry')
    if has_steer:
        reg_lines.append('        SelectionRegistry.register(playerId, new Steer());')
        wired.append('S=SelectionRegistry')
    wired_str = ' + '.join(wired)
    reg_lines.append(
        f'        System.err.println("{DRIVER_REGISTERED_MARKER} playerId=" + playerId\n'
        f'                + " ({wired_str}) arch={spec.archetype}");'
    )
    register_block = '\n'.join(reg_lines)

    mull_doc = ''
    if spec.mulligan_note:
        mull_doc = (
            '\n     *\n     * <p>Mulligan (keepable hands, primer-derived — documentation only;\n'
            '     * the frozen seam has no mulligan registry to bind it to): '
            f'{spec.mulligan_note}</p>'
        )

    parts: list[str] = []
    parts.append(f'package {pkg};\n')
    parts.append('\n'.join(imports))
    parts.append('')
    parts.append('/**')
    parts.append(f' * Generated in-search QUAD driver ({spec.name}) — archetype: {spec.archetype}.')
    parts.append(' *')
    parts.append(' * <p>Registers a quad (Φ, P, macro, S) by {@code playerId} into the dist seam')
    parts.append(' * registries; PlayerA stays a plain ComputerPlayer7 and any unregistered seat is')
    parts.append(f' * pure CP7. Loaded via -Dmakemagic.driver={pkg}.Driver.</p>{mull_doc}')
    parts.append(' */')
    parts.append('public final class Driver {')
    parts.append('')
    parts.append('    private Driver() {')
    parts.append('    }')
    parts.append('')
    parts.append('    /** Wire this deck\'s quad for {@code playerId}. Invoked by XMageBatch on PlayerA only. */')
    parts.append('    public static void register(UUID playerId) {')
    parts.append(register_block)
    parts.append('    }')
    parts.append('')
    parts.append('    // ---- Φ: bounded monotone potential toward the gameplan --------------------------')
    parts.append('    private static int phi(Game game, UUID pid) {')
    parts.append('        Player me = game.getPlayer(pid);')
    parts.append('        if (me == null) {')
    parts.append('            return 0;')
    parts.append('        }')
    parts.append(_indent(spec.phi_body, 8))
    parts.append('    }')
    if spec.helpers.strip():
        parts.append('')
        parts.append('    // ---- shared helpers -------------------------------------------------------------')
        parts.append(_indent(spec.helpers, 4))
    if has_macro:
        macro = spec.macro
        assert macro is not None
        parts.append('')
        parts.append('    // ---- macro (+ P): the deterministic win sequence, gated by P --------------------')
        parts.append('    private static final class Macro implements ComboMacro {')
        parts.append('        @Override')
        parts.append('        public boolean applicable(Game game, UUID pid) {')
        parts.append(_indent(macro.applicable_body, 12))
        parts.append('        }')
        parts.append('')
        parts.append('        @Override')
        parts.append('        public void apply(Game game, UUID pid) {')
        parts.append('            // STANDARD slot-exercise marker (emitter-injected): the seam invokes')
        parts.append('            // apply() ONLY to execute the deterministic win, so entry == the macro fired.')
        parts.append(f'            System.err.println("{DRIVER_MACRO_FIRED_MARKER} pid=" + pid);')
        parts.append(_indent(macro.apply_body, 12))
        parts.append('        }')
        parts.append('    }')
    if has_steer:
        steer = spec.steer
        assert steer is not None
        parts.append('')
        parts.append('    // ---- S: category-comprehensive selection steer ----------------------------------')
        parts.append('    private static final class Steer implements SelectionSteer {')
        parts.append('        @Override')
        parts.append('        public boolean apply(Game game, UUID pid, Cards cards, TargetCard target,')
        parts.append('                Ability source, boolean useAddTarget) {')
        parts.append('            // Wrap the author body so the STANDARD steer marker (emitter-injected) fires')
        parts.append('            // ONLY on a true return (a target actually added), never on a defer to CP7.')
        parts.append('            boolean fired = steer(game, pid, cards, target, source, useAddTarget);')
        parts.append('            if (fired) {')
        parts.append(f'                System.err.println("{DRIVER_STEER_FIRED_MARKER} pid=" + pid);')
        parts.append('            }')
        parts.append('            return fired;')
        parts.append('        }')
        parts.append('')
        parts.append('        private boolean steer(Game game, UUID pid, Cards cards, TargetCard target,')
        parts.append('                Ability source, boolean useAddTarget) {')
        parts.append(_indent(steer.apply_body, 12))
        parts.append('        }')
        parts.append('    }')
    parts.append('}')
    return '\n'.join(parts) + '\n'


# --------------------------------------------------------------------------- #
# The do-not-own guardrails (AC8) — the §7.1 discipline, enforced on the quad.  #
# --------------------------------------------------------------------------- #


class GuardrailViolation(ValueError):
    """A rendered quad reaches for something the design §7.1 do-not-own list forbids."""


#: (regex over the rendered source, human message). Ordered most-specific first. The macro's
#: real fire + the S steer run at the REAL seat, so they must own the noun and defer the verb:
#: never re-enter the turn loop (``priority()``) or copy the handed game (``copy()``), never own
#: combat / attacker selection or land drops. Matched by token so legitimate reads
#: (``isLand``, ``getAllActivePermanents``) never trip.
_GUARDRAILS: tuple[tuple[str, str], ...] = (
    (r'\.priority\s*\(', 'a quad slot must not call priority() on the handed sim game '
                         '(own the noun, defer the verb — drive the outcome with bounded state moves)'),
    (r'\.copy\s*\(', 'a quad slot must not call copy() on the handed game '
                     '(it would leak driver logic into nested search copies)'),
    (r'\b(?:selectAttackers|selectBlockers|declareAttacker\w*|forceAttack\w*)\b',
     'a quad must not own combat / attacker selection (the force-attack HARD RULE — CP7 owns combat)'),
    (r'\bplayLand\w*\s*\(', 'a quad must not own land drops / curve sequencing (CP7 curves out)'),
)


def check_quad_guardrails(rendered_source: str) -> None:
    """Statically enforce the §7.1 do-not-own discipline over a rendered quad ``Driver.java``.

    Raises :class:`GuardrailViolation` on the first forbidden construct (macro ``apply`` calling
    ``priority()``/``copy()`` on the handed sim; any slot owning combat/attacker selection or
    land drops). A clean quad returns ``None``. This is the AC8 gate the authoring skill runs
    before ECJ-compile. It is a coarse static screen — the never-worse behavioral gate (P5) is
    the ultimate arbiter — but it catches the classic regressions cheaply and deterministically.
    """
    for pattern, message in _GUARDRAILS:
        if re.search(pattern, rendered_source):
            raise GuardrailViolation(message)


# --------------------------------------------------------------------------- #
# Worked example #1 (PROACTIVE) — the Jeleva / Thassa's Oracle spell combo.     #
# Mirrors reference_drivers/JelevaThoracleReferenceDriver.java (the proven      #
# hand-authored quad) as a QuadSpec: Φ (potential toward assembly), macro       #
# (deterministic Thoracle win) gated by P (kill executable now), S (steer a     #
# tutor to the missing combo half). This is the emitter's positive control.     #
# --------------------------------------------------------------------------- #

_JELEVA_HELPERS = '''\
private static int untappedLands(Game game, UUID pid) {
    int n = 0;
    for (Permanent p : game.getBattlefield().getAllActivePermanents(pid)) {
        if (p.isLand(game) && !p.isTapped()) {
            n++;
        }
    }
    return n;
}'''

_JELEVA_PHI = '''\
boolean hasOracle = false;
boolean hasExile = false;
int pieces = 0;
int tutors = 0;
for (Card c : me.getHand().getCards(game)) {
    String n = c.getName();
    if ("Thassa's Oracle".equals(n)) {
        hasOracle = true;
        pieces++;
    } else if ("Demonic Consultation".equals(n) || "Tainted Pact".equals(n)) {
        hasExile = true;
        pieces++;
    } else if ("Demonic Tutor".equals(n) || "Vampiric Tutor".equals(n)
            || "Mystical Tutor".equals(n) || "Imperial Seal".equals(n)) {
        tutors++;
    }
}
int mana = untappedLands(game, pid);
int score = pieces * 40000 + tutors * 40000;
if (hasOracle || hasExile) {
    score += 300000;
}
if (tutors > 0 && (!hasOracle || !hasExile) && mana >= 1) {
    score += 400000;
}
if (hasOracle && hasExile) {
    score += 700000;
    if (mana >= 3) {
        score += 250000;
    }
}
if (hasOracle && me.getLibrary().size() == 0) {
    score += 1200000;
}
return score;'''

_JELEVA_MACRO_APPLICABLE = '''\
if (game.checkIfGameIsOver()) {
    return false;
}
Player me = game.getPlayer(pid);
if (me == null) {
    return false;
}
if (!pid.equals(game.getActivePlayerId()) || !game.getStack().isEmpty()) {
    return false;
}
boolean oracle = false;
boolean exile = false;
for (Card c : me.getHand().getCards(game)) {
    String n = c.getName();
    if ("Thassa's Oracle".equals(n)) {
        oracle = true;
    } else if ("Demonic Consultation".equals(n) || "Tainted Pact".equals(n)) {
        exile = true;
    }
}
if (!oracle) {
    return false;
}
if (me.getLibrary().size() == 0) {
    return true;
}
return exile && untappedLands(game, pid) >= 3;'''

_JELEVA_MACRO_APPLY = '''\
Player me = game.getPlayer(pid);
if (me == null) {
    return;
}
List<Card> lib = new ArrayList<>(me.getLibrary().getCards(game));
if (!lib.isEmpty()) {
    me.moveCards(new CardsImpl(lib), Zone.EXILED, null, game);
    game.applyEffects();
}
Card oracle = null;
for (Card c : me.getHand().getCards(game)) {
    if ("Thassa's Oracle".equals(c.getName())) {
        oracle = c;
        break;
    }
}
if (oracle != null) {
    me.moveCards(oracle, Zone.BATTLEFIELD, null, game);
    game.applyEffects();
    game.checkStateAndTriggered();
    int guard = 0;
    while (!game.getStack().isEmpty() && guard++ < ComboMacro.PROBE_MAX_STEPS) {
        game.getStack().resolve(game);
        game.applyEffects();
        game.checkStateAndTriggered();
        if (game.checkIfGameIsOver()) {
            break;
        }
    }
}
if (!game.checkIfGameIsOver()) {
    for (UUID opp : game.getOpponents(pid)) {
        Player o = game.getPlayer(opp);
        if (o != null) {
            o.lost(game);
        }
    }
    game.applyEffects();
    game.checkStateAndTriggered();
}'''

_JELEVA_STEER = '''\
if (source == null || cards == null || target == null) {
    return false;
}
if (!pid.equals(source.getControllerId())) {
    return false;
}
Player me = game.getPlayer(pid);
if (me == null) {
    return false;
}
boolean hasOracle = false;
boolean hasExile = false;
for (Card c : me.getHand().getCards(game)) {
    String n = c.getName();
    if ("Thassa's Oracle".equals(n)) {
        hasOracle = true;
    } else if ("Demonic Consultation".equals(n) || "Tainted Pact".equals(n)) {
        hasExile = true;
    }
}
if (hasOracle && hasExile) {
    return false;
}
String[] wants = !hasOracle
        ? new String[]{"Thassa's Oracle"}
        : new String[]{"Demonic Consultation", "Tainted Pact"};
for (String want : wants) {
    for (Card c : me.getLibrary().getCards(game)) {
        if (!want.equals(c.getName())) {
            continue;
        }
        UUID id = c.getId();
        if (!cards.contains(id)) {
            continue;
        }
        if (!target.canTarget(pid, id, source, cards, game)) {
            continue;
        }
        if (useAddTarget) {
            target.addTarget(id, source, game);
        } else {
            target.add(id, game);
        }
        return true;
    }
}
return false;'''

#: The Jeleva/Thoracle quad — the emitter's PROACTIVE positive control (mirrors the proven
#: hand-authored reference driver). Φ ← Gameplan, macro ← Win Condition, P ← Assembly,
#: S ← Sequencing (steer a tutor to the missing combo half).
JELEVA_QUAD_SPEC = QuadSpec(
    name='jeleva-thoracle-spell-combo',
    archetype='proactive',
    phi_body=_JELEVA_PHI,
    macro=MacroSpec(applicable_body=_JELEVA_MACRO_APPLICABLE, apply_body=_JELEVA_MACRO_APPLY),
    steer=SteerSpec(apply_body=_JELEVA_STEER),
    helpers=_JELEVA_HELPERS,
    imports=(
        'import java.util.ArrayList;',
        'import java.util.List;',
        '',
        'import mage.cards.Card;',
        'import mage.cards.CardsImpl;',
        'import mage.constants.Zone;',
        'import mage.game.permanent.Permanent;',
    ),
    mulligan_note='keep any hand with a combo half + a tutor and >=3 lands; ship no-landers.',
)


# --------------------------------------------------------------------------- #
# Worked example #2 (Φ-ONLY REACTIVE) — a control/hold-up deck (Shorikai-shape).#
# No macro (no proactive kill to execute), no S: only Φ, a potential that       #
# rewards holding interaction + mana open — the reactive readiness signal.      #
# The gate runs this in Φ-only mode (never-worse-solo floor, defended lens).    #
# --------------------------------------------------------------------------- #

_REACTIVE_PHI = '''\
int openMana = 0;
for (Permanent p : game.getBattlefield().getAllActivePermanents(pid)) {
    if (p.isLand(game) && !p.isTapped()) {
        openMana++;
    }
}
// Reactive readiness: reward keeping cards + mana OPEN (able to answer on the opp's turn),
// bounded well within +/-1e6 so the win terminal still dominates.
int cardsInHand = me.getHand().size();
int score = openMana * 40000 + cardsInHand * 20000;
if (openMana >= 2 && cardsInHand >= 2) {
    score += 200000;
}
return score;'''

#: A Φ-only reactive quad — the emitter's REACTIVE control. Emits Φ ONLY: no macro/P/S. Proves
#: the scaffold registers just ``DriverBonus`` and omits the ``Macro``/``Steer`` inner classes.
SHORIKAI_REACTIVE_QUAD_SPEC = QuadSpec(
    name='shorikai-control-holdup',
    archetype='reactive',
    phi_body=_REACTIVE_PHI,
    macro=None,
    steer=None,
    imports=('import mage.game.permanent.Permanent;',),
    mulligan_note='keep hands with >=2 lands and an early interactive spell; ship all-action no-mana keeps.',
)
