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
    from pipeline.transforms.combo_detect import Combo

__all__ = (
    # Names sorted (ruff RUF022).
    'ARCHETYPES',
    'DRIVER_MACRO_FIRED_MARKER',
    'DRIVER_MULLIGAN_MARKER',
    'DRIVER_PACKAGE_ROOT',
    'DRIVER_REGISTERED_MARKER',
    'DRIVER_SIMPLE_CLASS',
    'DRIVER_STEER_FIRED_MARKER',
    'JELEVA_QUAD_SPEC',
    'SHORIKAI_REACTIVE_QUAD_SPEC',
    'GuardrailViolation',
    'MacroSpec',
    'MulliganSpec',
    'QuadSpec',
    'SteerSpec',
    'check_quad_guardrails',
    'driver_fqcn',
    'driver_package',
    'render_quad_driver',
    'seed_quad_from_combo',
    'validate_archetype',
)

#: The combo-litmus archetype vocabulary (design §5 rules 1 + 4). ``'drive-dedicated'`` and
#: ``'drive-capable'`` are the two DRIVE Φ-modes (full combo-Φ staging vs thin-Φ + macro/P/S);
#: ``'thin'`` is bare CP7 (no in-deck win-combo). Replaces the retired proactive/reactive spine
#: as the top-level classifier output.
ARCHETYPES: tuple[str, ...] = ('drive-dedicated', 'drive-capable', 'thin')


def validate_archetype(archetype: str) -> str:
    """Return ``archetype`` if it is one of :data:`ARCHETYPES`, else raise ``ValueError``.

    The combo-litmus factories (:meth:`QuadSpec.thin`, :func:`seed_quad_from_combo`) route every
    archetype through this so a stray legacy value (e.g. ``'proactive'``) fails loudly. The free
    :class:`QuadSpec` constructor is intentionally NOT validated — the worked-example reference
    specs still carry their documentary ``'proactive'`` / ``'reactive'`` labels.
    """
    if archetype not in ARCHETYPES:
        raise ValueError(f'archetype {archetype!r} is not one of {ARCHETYPES}')
    return archetype

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
#: The mulligan slot's real-fire marker. The **dist itself** (patch ``0005``,
#: ``ComputerPlayer.chooseMulligan``) prints the AUTHORITATIVE ``DRIVER_MULLIGAN name=<seat>
#: ship=<bool>`` on stderr the instant a registered seat makes its REAL pre-game keep/ship
#: decision (guarded on ``!game.isSimulation()`` after the full-hand/test/Momir early-returns), so
#: an unregistered seat is byte-identical to prior CP7 and never emits it. The emitter also injects
#: a detail line (the same prefix) at the top of the steer body for debugging; the gate keys on the
#: marker's presence to record :attr:`~pipeline.sim.driver_gate.GateResult.mulligan_fired`.
DRIVER_MULLIGAN_MARKER = 'DRIVER_MULLIGAN'


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
class MulliganSpec:
    """The mulligan slot (the quad's 5th dimension): the opening-hand keep/ship decision.

    ``ship_body`` (from the primer **Mulligan / keepable hands** section) is the body of
    ``boolean shipHand(Game game, UUID pid)`` — return ``true`` to SHIP (take a mulligan on)
    this opening hand, ``false`` to KEEP. The scaffold prepends a non-null ``Player me`` guard
    (returning ``false`` — keep — on a torn-down player) so the body may read ``me.getHand()``.

    This is a REAL-SEAT decision (the dist consults it only on the real pre-game hand, never on
    a search copy), so the design §7.1 three-beat guard→act→defer discipline applies: own the
    keep/ship of the opening hand (ship no-land/flood, keep a hand with a plan piece), and defer
    WHICH cards to bottom (London) to CP — v1 is the keep/ship boolean only.
    """

    ship_body: str


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
    ``mulligan`` is the OPTIONAL 5th-dimension slot (:class:`MulliganSpec`): present ⇒ the
    scaffold emits a ``MulliganSteer`` inner class + ``MulliganRegistry.register`` so the dist's
    ``chooseMulligan`` hook (patch ``0005``) consults it on the real opening hand; ``None`` ⇒ no
    mulligan registration (the deck keeps CP7's default land-count heuristic). ``mulligan_note``
    records the primer **Mulligan** guidance as prose in the class javadoc (independent of the
    live slot — keep it as the human-readable intent even when ``mulligan`` is wired).
    """

    name: str
    #: The combo-litmus archetype — one of :data:`ARCHETYPES` for combo-litmus-authored quads
    #: (``'thin'`` / ``'drive-capable'`` / ``'drive-dedicated'``). Documentary in the emitter (it
    #: rides in the ``DRIVER_REGISTERED`` breadcrumb); the *presence of a macro* is the machine
    #: signal for DRIVE. (The worked-example reference specs keep legacy ``'proactive'`` /
    #: ``'reactive'`` labels — the free constructor is unvalidated by design.)
    archetype: str
    phi_body: str
    macro: MacroSpec | None = None
    steer: SteerSpec | None = None
    mulligan: MulliganSpec | None = None
    helpers: str = ''
    imports: tuple[str, ...] = ()
    mulligan_note: str = ''

    @classmethod
    def thin(
        cls,
        name: str,
        *,
        archetype: str = 'thin',
        mulligan: MulliganSpec | None = None,
        mulligan_note: str = '',
    ) -> QuadSpec:
        """The THIN driver (combo-litmus rule 1: no in-deck win-combo) — bare CP7.

        Renders a NEUTRAL quad: ``phi_body='return 0;'`` (a no-op Φ that adds nothing to any
        leaf), NO macro and NO steer. An OPTIONAL mulligan slot may be supplied (the ablation
        left it optional), but the default is a pure Φ=0 driver behaviorally identical to bare
        CP7. ``archetype`` must be ``'thin'`` (validated); it is a keyword only so a mistaken
        DRIVE value can't slip in positionally.
        """
        validate_archetype(archetype)
        if archetype != 'thin':
            raise ValueError(f"QuadSpec.thin requires archetype='thin', got {archetype!r}")
        return cls(
            name=name,
            archetype=archetype,
            phi_body='return 0;',
            macro=None,
            steer=None,
            mulligan=mulligan,
            mulligan_note=mulligan_note,
        )


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
    has_mulligan = spec.mulligan is not None

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
    if has_mulligan:
        imports += [
            'import mage.player.ai.score.MulliganRegistry;',
            'import mage.player.ai.score.MulliganSteer;',
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
    if has_mulligan:
        reg_lines.append('        MulliganRegistry.register(playerId, new Mull());')
        wired.append('mull=MulliganRegistry')
    wired_str = ' + '.join(wired)
    reg_lines.append(
        f'        System.err.println("{DRIVER_REGISTERED_MARKER} playerId=" + playerId\n'
        f'                + " ({wired_str}) arch={spec.archetype}");'
    )
    register_block = '\n'.join(reg_lines)

    mull_doc = ''
    if spec.mulligan_note:
        binding = (
            'wired into MulliganRegistry — the dist consults it on the real opening hand'
            if has_mulligan
            else 'documentation only — this quad keeps CP7 default mulligan'
        )
        mull_doc = (
            f'\n     *\n     * <p>Mulligan (keepable hands, primer-derived; {binding}): '
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
        parts.append('            // REACHABILITY marker (emitter-injected): the seam invokes apply() on THROWAWAY')
        parts.append('            // search copies at every node where P holds AND in real act(), so entry means the')
        parts.append('            // macro is REACHABLE, not that it executed to win. True execution is proven by the')
        parts.append('            // frozen dist\'s MACRO_FIRE_REAL (emitted only from the real act() commit).')
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
    if has_mulligan:
        mulligan = spec.mulligan
        assert mulligan is not None
        parts.append('')
        parts.append('    // ---- mulligan (5th dimension): the opening-hand keep/ship decision --------------')
        parts.append('    private static final class Mull implements MulliganSteer {')
        parts.append('        @Override')
        parts.append('        public boolean shipHand(Game game, UUID pid) {')
        parts.append('            // The dist (patch 0005 chooseMulligan) emits the AUTHORITATIVE DRIVER_MULLIGAN')
        parts.append('            // line on the real pre-game decision; this detail line aids debugging.')
        parts.append('            Player me = game.getPlayer(pid);')
        parts.append('            if (me == null) {')
        parts.append('                return false;')
        parts.append('            }')
        parts.append(f'            System.err.println("{DRIVER_MULLIGAN_MARKER} (author) pid=" + pid')
        parts.append('                    + " handSize=" + me.getHand().size());')
        parts.append(_indent(mulligan.ship_body, 12))
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
# Rule-3 combo → quad seeding (DETERMINISTIC template fill).                    #
#                                                                              #
# From the chosen Combo: card_names → P (all pieces present + castable) + S     #
# (fetch a still-missing piece via any tutor/search) + a staging Φ (dedicated); #
# result → a bounded macro SCAFFOLD (the deterministic win enactment — a        #
# clearly-marked TODO the deck author refines per-card downstream). The author   #
# does NOT invent the line; the template is filled from the Combo verbatim.     #
# --------------------------------------------------------------------------- #


def _java_str(literal: str) -> str:
    """A Python string → a safe Java double-quoted string literal (escapes ``\\`` and ``"``)."""
    escaped = literal.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _java_name_set(card_names: tuple[str, ...]) -> str:
    """Render ``card_names`` as a ``java.util.Arrays.asList(...)`` argument list of literals."""
    return ', '.join(_java_str(n) for n in card_names)


def _seed_phi_body(card_names: tuple[str, ...], *, dedicated: bool) -> str:
    """Φ body: a piece-staging potential (DEDICATED) or a neutral Φ=0 (CAPABLE, serendipitous)."""
    if not dedicated:
        # COMBO-CAPABLE: thin Φ=0 + macro/P/S (no distortion of the base plan).
        return 'return 0;'
    names = _java_name_set(card_names)
    total = len(card_names)
    return f'''\
// DEDICATED combo-Φ (rule 4): a bounded, monotone potential toward assembling the win pieces.
java.util.Set<String> want = new java.util.HashSet<>(java.util.Arrays.asList({names}));
int pieces = 0;
for (Card c : me.getHand().getCards(game)) {{
    if (want.contains(c.getName())) {{
        pieces++;
    }}
}}
int score = pieces * 40000;
if (pieces == {total}) {{
    score += 700000;
}}
return score;'''


#: The assembly-completion bonus multiplier for the opportunistic nudge Φ: the all-pieces-present
#: bonus is ``alpha * NUDGE_ASSEMBLY_BONUS_MULT``. Kept small enough that the whole nudge stays a
#: bounded tie-breaker (max ``total*alpha + alpha*MULT`` — well under ±1e6 at the swept alphas),
#: NOT a dominant term like the DEDICATED Φ (which hard-codes 40000/piece + 700000 all-present).
NUDGE_ASSEMBLY_BONUS_MULT = 10


def _nudge_phi_body(card_names: tuple[str, ...], *, alpha: int) -> str:
    """OPPORTUNISTIC combo-Φ: a SMALL, bounded nudge (magnitude ``alpha``) toward holding/assembling
    the win pieces — the missing middle of the magnitude axis between thin (Φ=0) and DEDICATED
    (Φ=40000/piece, combo-is-the-plan).

    Three cheapness properties (this is also the answer to the 3.2x nudge-cost tax):

    * **Cheap early-out gate** — a first pass asks "is even ONE combo piece live (in my hand or on
      my battlefield)?"; if not, ``return 0`` immediately, skipping the assembly scoring on the
      ~majority of search nodes where the combo is not in play. Reuses the same piece-presence
      logic as the DEDICATED Φ / the P precondition (``want.contains(name)`` over hand + battlefield).
    * **Bounded magnitude alpha** — the score is ``alpha`` per distinct piece present plus an
      ``alpha * NUDGE_ASSEMBLY_BONUS_MULT`` all-present bonus. It is a tie-breaker that biases the
      search toward holding/playing pieces and completing assembly, NOT a dominant term; ``alpha=0``
      is byte-identical to thin (``return 0;``), and even at the large end the total stays bounded
      well under ±1e6.
    * **Per-turn memoization** — deliberately NOT applied across nodes: Φ is evaluated on THROWAWAY
      minimax search COPIES of the game (each node is a distinct, mutated ``Game``), so a static
      cross-node cache keyed on turn would return STALE assembly facts for a sibling node whose board
      differs. Correctness forbids it. Instead the cheapness comes from the early-out (skips the body
      entirely on most nodes) + a bounded O(hand + battlefield) body after it. Documented here per the
      seam's memoization guidance.
    """
    if alpha < 0:
        raise ValueError(f'nudge alpha must be >= 0 (got {alpha})')
    if alpha == 0:
        # alpha=0 is the axis origin: identical to thin Φ (does nothing — no distortion of the base plan).
        return 'return 0;'
    names = _java_name_set(card_names)
    total = len(card_names)
    bonus = alpha * NUDGE_ASSEMBLY_BONUS_MULT
    return f'''\
// OPPORTUNISTIC combo-Φ (bounded nudge magnitude alpha={alpha}): a SMALL tie-breaker toward
// holding/assembling the win pieces, NOT a dominant term. The deck plays its normal game and
// captures the combo when it naturally comes together (the macro fires when P holds).
java.util.Set<String> want = new java.util.HashSet<>(java.util.Arrays.asList({names}));
// CHEAP EARLY-OUT: if not one combo piece is live (hand or battlefield), the combo is not in play
// at this node — skip the assembly scoring entirely (the common case, killing the nudge cost tax).
boolean anyLive = false;
for (Card c : me.getHand().getCards(game)) {{
    if (want.contains(c.getName())) {{
        anyLive = true;
        break;
    }}
}}
if (!anyLive) {{
    for (Permanent p : game.getBattlefield().getAllActivePermanents(pid)) {{
        if (want.contains(p.getName())) {{
            anyLive = true;
            break;
        }}
    }}
}}
if (!anyLive) {{
    return 0;
}}
// Assembly progress: distinct pieces present in hand or on the battlefield (bounded O(hand+board)).
java.util.Set<String> have = new java.util.HashSet<>();
for (Card c : me.getHand().getCards(game)) {{
    if (want.contains(c.getName())) {{
        have.add(c.getName());
    }}
}}
for (Permanent p : game.getBattlefield().getAllActivePermanents(pid)) {{
    if (want.contains(p.getName())) {{
        have.add(p.getName());
    }}
}}
int pieces = have.size();
int score = pieces * {alpha};
if (pieces == {total}) {{
    score += {bonus};
}}
return score;'''


def _seed_applicable_body(card_names: tuple[str, ...]) -> str:
    """P body: all combo pieces present (hand or battlefield) & the kill is castable this turn."""
    names = _java_name_set(card_names)
    total = len(card_names)
    return f'''\
if (game.checkIfGameIsOver()) {{
    return false;
}}
Player me = game.getPlayer(pid);
if (me == null) {{
    return false;
}}
if (!pid.equals(game.getActivePlayerId()) || !game.getStack().isEmpty()) {{
    return false;
}}
// All combo pieces must be present (in hand or on the battlefield) to fire this turn.
java.util.Set<String> want = new java.util.HashSet<>(java.util.Arrays.asList({names}));
java.util.Set<String> have = new java.util.HashSet<>();
for (Card c : me.getHand().getCards(game)) {{
    if (want.contains(c.getName())) {{
        have.add(c.getName());
    }}
}}
for (Permanent p : game.getBattlefield().getAllActivePermanents(pid)) {{
    if (want.contains(p.getName())) {{
        have.add(p.getName());
    }}
}}
return have.size() == {total};'''


def _seed_apply_body(result: str, card_names: tuple[str, ...]) -> str:
    """Macro body: a BOUNDED, LEGAL-ACTIONS-ONLY win enactment (author refines the cast order).

    The no-terminal-API rule (enforced by :mod:`pipeline.sim.driver_lint` at gate + load time):
    a driver may ONLY enqueue LEGAL game actions — every terminal state must come from the rules
    engine. So the scaffold PLAYS the line — it casts each combo piece it holds through the
    engine's real cast path (``chooseAbilityForCast`` + ``cast``), then resolves the stack in a
    BOUNDED loop — and NEVER calls ``lost()``/``won()``/``setWinner()``/``end()`` or
    ``moveCards``-fabricates a zone. Because the spells go on the stack, the opponent gets
    priority (the line is CONTESTABLE — a held counterspell can answer it); the win, if it comes,
    is the engine's, not an assertion. An unrefined line that fails to actually win simply won't
    emit ``MACRO_FIRE_REAL`` and the gate rejects it — the honest outcome, not a fake pass.
    """
    # result rides in a // comment — strip newlines so it can't break out of the line comment.
    result_comment = ' '.join((result or '(unspecified win)').split())
    names = _java_name_set(card_names)
    return f'''\
Player me = game.getPlayer(pid);
if (me == null) {{
    return;
}}
// LEGAL-ACTIONS-ONLY win enactment (no-terminal-API rule): cast each combo piece from hand
// through the rules engine, then resolve the stack BOUNDED (<= ComboMacro.PROBE_MAX_STEPS).
// The line is PLAYED and CONTESTABLE (spells hit the stack; the opponent gets priority) — the
// terminal comes from the engine, never from lost()/won()/setWinner() or moveCards-fabrication.
// TODO(author): refine the cast ORDER + any targeting for this specific line — {result_comment}
java.util.Set<String> pieces = new java.util.HashSet<>(java.util.Arrays.asList({names}));
for (Card c : new java.util.ArrayList<>(me.getHand().getCards(game))) {{
    if (!pieces.contains(c.getName())) {{
        continue;
    }}
    mage.abilities.SpellAbility sa = me.chooseAbilityForCast(c, game, false);
    if (sa != null) {{
        me.cast(sa, game, false, null);
        game.applyEffects();
    }}
    int guard = 0;
    while (!game.getStack().isEmpty() && guard++ < ComboMacro.PROBE_MAX_STEPS) {{
        game.getStack().resolve(game);
        game.applyEffects();
        game.checkStateAndTriggered();
        if (game.checkIfGameIsOver()) {{
            return;
        }}
    }}
}}'''


def _seed_steer_body(card_names: tuple[str, ...]) -> str:
    """S body: steer MY OWN tutor/search toward any still-missing combo piece (category-agnostic
    over the piece set — fetch whichever piece the hand lacks)."""
    names = _java_name_set(card_names)
    return f'''\
if (source == null || cards == null || target == null) {{
    return false;
}}
if (!pid.equals(source.getControllerId())) {{
    return false;
}}
Player me = game.getPlayer(pid);
if (me == null) {{
    return false;
}}
// Fetch whichever combo piece is still MISSING from hand (any tutor/library search steer).
java.util.Set<String> want = new java.util.HashSet<>(java.util.Arrays.asList({names}));
for (Card c : me.getHand().getCards(game)) {{
    want.remove(c.getName());
}}
if (want.isEmpty()) {{
    return false;
}}
for (Card c : me.getLibrary().getCards(game)) {{
    if (!want.contains(c.getName())) {{
        continue;
    }}
    UUID id = c.getId();
    if (!cards.contains(id)) {{
        continue;
    }}
    if (!target.canTarget(pid, id, source, cards, game)) {{
        continue;
    }}
    if (useAddTarget) {{
        target.addTarget(id, source, game);
    }} else {{
        target.add(id, game);
    }}
    return true;
}}
return false;'''


def seed_quad_from_combo(combo: Combo, *, archetype: str) -> QuadSpec:
    """Rule-3 DETERMINISTIC seed: fill the fixed quad template from a detected win ``Combo``.

    ``combo.card_names`` generate **P** (``applicable`` — all pieces present + castable this turn)
    and **S** (steer a tutor/search to a still-missing piece); ``combo.result`` seeds the **macro**
    (a bounded, ECJ-compiling win-enactment SCAFFOLD with a TODO for the true per-card line — the
    author does NOT invent it here). Φ is a piece-staging potential when ``archetype`` is
    ``'drive-dedicated'`` (rule 4 DEDICATED) or a neutral ``Φ=0`` when ``'drive-capable'``
    (COMBO-CAPABLE — macro/P/S with no base-plan distortion). ``archetype`` must be a DRIVE value
    (``'drive-dedicated'`` / ``'drive-capable'``); ``'thin'`` is rejected (a thin deck uses
    :meth:`QuadSpec.thin`, not a seed).
    """
    validate_archetype(archetype)
    if archetype == 'thin':
        raise ValueError("seed_quad_from_combo needs a DRIVE archetype, not 'thin'")
    dedicated = archetype == 'drive-dedicated'
    return QuadSpec(
        name=f'combo-{combo.variant_id}',
        archetype=archetype,
        phi_body=_seed_phi_body(combo.card_names, dedicated=dedicated),
        macro=MacroSpec(
            applicable_body=_seed_applicable_body(combo.card_names),
            apply_body=_seed_apply_body(combo.result, combo.card_names),
        ),
        steer=SteerSpec(apply_body=_seed_steer_body(combo.card_names)),
        imports=(
            'import mage.cards.Card;',
            'import mage.game.permanent.Permanent;',
        ),
        mulligan_note=f'seeded from combo {combo.variant_id}: {", ".join(combo.card_names)}.',
    )


def seed_nudge_quad(combo: Combo, *, alpha: int) -> QuadSpec:
    """OPPORTUNISTIC seed: the rule-3 macro/P/S (fires the win when pieces assemble+cast) plus an
    opportunistic **nudge Φ** of magnitude ``alpha`` — the missing middle of the Φ magnitude axis.

    This is the quad the opportunistic-mode bet rests on: the macro is the SAME rule-3 win-enactment
    as :func:`seed_quad_from_combo`, so the deck captures the combo when it naturally comes together,
    but the Φ does NOT force assembly — it is only a small bounded tie-breaker (see
    :func:`_nudge_phi_body`). Sweeping ``alpha`` walks the single axis: ``alpha=0`` is thin
    (Φ=0 + macro), a small/medium ``alpha`` is the opportunistic middle, and a large ``alpha``
    approaches the DEDICATED end (big staging Φ). ``alpha`` must be ``>= 0``.
    """
    if alpha < 0:
        raise ValueError(f'nudge alpha must be >= 0 (got {alpha})')
    # archetype is documentary (rides the DRIVER_REGISTERED breadcrumb only): 'drive-capable' at the
    # thin origin (Φ=0), 'drive-dedicated' once the Φ carries an assembly gradient.
    archetype = 'drive-capable' if alpha == 0 else 'drive-dedicated'
    return QuadSpec(
        name=f'combo-{combo.variant_id}-nudge{alpha}',
        archetype=archetype,
        phi_body=_nudge_phi_body(combo.card_names, alpha=alpha),
        macro=MacroSpec(
            applicable_body=_seed_applicable_body(combo.card_names),
            apply_body=_seed_apply_body(combo.result, combo.card_names),
        ),
        steer=SteerSpec(apply_body=_seed_steer_body(combo.card_names)),
        imports=(
            'import mage.cards.Card;',
            'import mage.game.permanent.Permanent;',
        ),
        mulligan_note=(
            f'opportunistic nudge (alpha={alpha}) seeded from combo {combo.variant_id}: '
            f'{", ".join(combo.card_names)}.'
        ),
    )


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
// LEGAL-ACTIONS-ONLY win enactment (no-terminal-API rule): PLAY the Consultation→Oracle line
// through the rules engine — cast the exile-your-library spell (Demonic Consultation / Tainted
// Pact) so its resolution empties the library, then cast Thassa's Oracle so ITS enters-the-
// battlefield trigger wins on an empty library. Every step goes on the stack (the opponent gets
// priority — a held counterspell can answer it) and the terminal is the ENGINE's. NEVER call
// lost()/won()/setWinner() or moveCards-fabricate a zone (see pipeline.sim.driver_lint).
java.util.List<String> castOrder = java.util.Arrays.asList(
        "Demonic Consultation", "Tainted Pact", "Thassa's Oracle");
for (String want : castOrder) {
    Card inHand = null;
    for (Card c : new ArrayList<>(me.getHand().getCards(game))) {
        if (want.equals(c.getName())) {
            inHand = c;
            break;
        }
    }
    if (inHand == null) {
        continue;
    }
    mage.abilities.SpellAbility sa = me.chooseAbilityForCast(inHand, game, false);
    if (sa != null) {
        me.cast(sa, game, false, null);
        game.applyEffects();
    }
    int guard = 0;
    while (!game.getStack().isEmpty() && guard++ < ComboMacro.PROBE_MAX_STEPS) {
        game.getStack().resolve(game);
        game.applyEffects();
        game.checkStateAndTriggered();
        if (game.checkIfGameIsOver()) {
            return;
        }
    }
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

_JELEVA_MULLIGAN = '''\
int lands = 0;
boolean hasPiece = false;
for (Card c : me.getHand().getCards(game)) {
    if (c.isLand(game)) {
        lands++;
    }
    String n = c.getName();
    if ("Thassa's Oracle".equals(n) || "Demonic Consultation".equals(n)
            || "Tainted Pact".equals(n) || "Demonic Tutor".equals(n)
            || "Vampiric Tutor".equals(n) || "Mystical Tutor".equals(n)
            || "Imperial Seal".equals(n)) {
        hasPiece = true;
    }
}
int size = me.getHand().size();
// Keep any hand with a plan piece on a workable land count (2..size-2).
if (hasPiece && lands >= 2 && lands <= size - 2) {
    return false;
}
// Ship land-screw (0-1 land) or flood (>= size-1 lands); otherwise keep.
return lands <= 1 || lands >= size - 1;'''

#: The Jeleva/Thoracle quad — the emitter's PROACTIVE positive control (mirrors the proven
#: hand-authored reference driver). Φ ← Gameplan, macro ← Win Condition, P ← Assembly,
#: S ← Sequencing (steer a tutor to the missing combo half), mulligan ← Mulligan (ship
#: screw/flood, keep a hand with a combo piece).
JELEVA_QUAD_SPEC = QuadSpec(
    name='jeleva-thoracle-spell-combo',
    archetype='proactive',
    phi_body=_JELEVA_PHI,
    macro=MacroSpec(applicable_body=_JELEVA_MACRO_APPLICABLE, apply_body=_JELEVA_MACRO_APPLY),
    steer=SteerSpec(apply_body=_JELEVA_STEER),
    mulligan=MulliganSpec(ship_body=_JELEVA_MULLIGAN),
    helpers=_JELEVA_HELPERS,
    imports=(
        'import java.util.ArrayList;',
        'import java.util.List;',
        '',
        'import mage.cards.Card;',
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
