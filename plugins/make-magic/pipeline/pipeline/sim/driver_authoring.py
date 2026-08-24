"""Phase 2 -- the thin per-deck driver AUTHORING surface (template + Mikaeus fill).

A per-deck driver is a separately-compiled ``ComputerPlayer7`` subclass that plugs
into the Phase-0 ``XMageBatch`` ``-Dmakemagic.driverA=<FQCN>`` seam (see
:mod:`pipeline.sim.drivers` for storage, :mod:`pipeline.sim.driver_gate` for the
compile/behavioral gate). This module owns ONE thing: turning a deck + a
:class:`LineSpec` (the deck's non-obvious line, authored by a human in Phase 2 or an
LLM in Phase 4) into a compilable ``Driver.java`` source string.

The spike's load-bearing discipline, baked into :data:`_DRIVER_TEMPLATE`:

  * The driver is a ``ComputerPlayer7`` subclass with the seam's required
    ``(String, RangeOfInfluence, int)`` ctor (+ a copy ctor for engine cloning). It
    does NOT override ``copy()``, so CP7's internal minimax simulations run as PURE
    CP7 -- the owned overrides fire only on the real player's actual decisions.
  * It OWNS only three things: a **guarded ``priority()``** that executes the deck's
    line and otherwise defers to ``super.priority()``; **target-steering scoped to
    the line**; a **light mulligan** for the line's key pieces.
  * HARD RULES (spike findings, non-negotiable): **NEVER force-attack** and never
    write an "own-main ⇒ never defer" branch. Everything outside the owned line --
    land drops, curve, ALL combat, removal, discard, the opponent's turn -- defers to
    CP7. A thin driver adds the line; it does not re-pilot the deck.

Instrumentation hook (a real constraint -- READ THIS): the standalone ``--solo``
harness does NOT emit the lab's ``comboFired``/``drawable`` statics (Phase 0 dropped
them). So the behavioral gate cannot read "the line fired" from the summary. Instead
the template bakes an idempotent ``markLineFired()`` that prints
``DRIVER_LINE_FIRED name=<name>`` to stderr the first time the driver executes its
owned line (mirroring Phase 0's ``DUMMY_DRIVER_LOADED``); the gate greps for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.contracts import Deck

__all__ = (
    'DRIVER_LINE_FIRED_MARKER',
    'DRIVER_PACKAGE_ROOT',
    'DRIVER_SIMPLE_CLASS',
    'MIKAEUS_LINE_SPEC',
    'LineSpec',
    'driver_fqcn',
    'driver_package',
    'render_driver',
)

#: The stderr marker the template emits (via ``markLineFired``) the first time the
#: driver runs its owned line -- the string :mod:`pipeline.sim.driver_gate` greps for.
DRIVER_LINE_FIRED_MARKER = 'DRIVER_LINE_FIRED'
#: The package prefix every generated driver lives under (per-deck leaf appended).
DRIVER_PACKAGE_ROOT = 'makemagic.driver'
#: The (fixed) simple class name; deck identity lives in the package leaf so every
#: driver's source is literally ``Driver.java`` (the file the gate writes + compiles).
DRIVER_SIMPLE_CLASS = 'Driver'


@dataclass(frozen=True)
class LineSpec:
    """The deck-specific content stamped into the thin-driver template.

    A ``LineSpec`` is *only* the owned line -- the template supplies the ctor, copy
    ctor, and ``markLineFired`` scaffold around it. ``members`` is the Java source for
    the owned overrides (a guarded ``priority``, the scoped ``chooseTarget`` steering,
    the light ``chooseMulligan``) plus any private helpers they need; it must call
    ``markLineFired()`` at the point the line actually executes. ``imports`` are the
    extra ``import`` lines those members reference (beyond the always-present
    ``ComputerPlayer7`` / ``RangeOfInfluence``).
    """

    name: str
    imports: tuple[str, ...]
    members: str = field(default='')


def _sanitize(uuid: str) -> str:
    """A deck uuid → a legal Java package-segment leaf (``d_<hex>``).

    The store's ``deck.uuid`` is a 32-char hex string, which cannot begin a Java
    identifier if it starts with a digit -- so prefix ``d_`` and replace any stray
    non-word char, keeping the driver's FQCN stable + legal across decks.
    """
    return 'd_' + re.sub(r'\W', '_', uuid)


def driver_package(deck: Deck) -> str:
    """``makemagic.driver.d_<uuid>`` -- the per-deck package the driver is emitted into."""
    return f'{DRIVER_PACKAGE_ROOT}.{_sanitize(deck.uuid)}'


def driver_fqcn(deck: Deck) -> str:
    """``makemagic.driver.d_<uuid>.Driver`` -- the FQCN the seam loads via reflection.

    Passed to ``-Dmakemagic.driverA`` and stamped into the driver's ``meta.json`` so
    the run path (:meth:`XMageEngine.goldfish`/``run_matchup``) and the gate agree on
    exactly which class to load."""
    return f'{driver_package(deck)}.{DRIVER_SIMPLE_CLASS}'


#: The thin-driver template. The ``@@TOKEN@@`` placeholders are filled by
#: :func:`render_driver` via plain string replacement (NOT str.format -- the Java body
#: contains ``{...}`` braces). Everything the SPIKE proved load-bearing lives here so a
#: per-deck fill (a :class:`LineSpec`) cannot forget it: the seam ctor, the copy ctor
#: (engine cloning), NO ``copy()`` override (sim runs pure CP7), and the idempotent
#: ``DRIVER_LINE_FIRED`` marker.
_DRIVER_TEMPLATE = '''\
package @@PACKAGE@@;

import mage.constants.RangeOfInfluence;
import mage.player.ai.ComputerPlayer7;
@@IMPORTS@@

/**
 * Generated THIN per-deck driver (@@NAME@@).
 *
 * Owns ONLY this deck's non-obvious line (a guarded priority(), scoped target
 * steering, a light mulligan) and defers EVERYTHING else -- land drops, curve, ALL
 * combat, removal, the opponent's turn -- to ComputerPlayer7. No copy override, so
 * CP7's internal minimax simulations run as pure CP7; the owned overrides fire only
 * on the real player's decisions. Loaded on PlayerA via -Dmakemagic.driverA.
 */
public class Driver extends ComputerPlayer7 {

    // Idempotent latch behind markLineFired() so the marker prints at most once.
    private boolean lineFired = false;

    public Driver(String name, RangeOfInfluence range, int skill) {
        super(name, range, skill);
    }

    // Copy ctor for engine cloning; the copy method is NOT overridden (sim stays pure CP7).
    public Driver(final Driver d) {
        super(d);
        this.lineFired = d.lineFired;
    }

    /**
     * Emit the behavioral gate's marker the FIRST time this driver executes its owned
     * line -- the standalone --solo harness has no comboFired static, so this stderr
     * marker (mirroring Phase 0's DUMMY_DRIVER_LOADED) is how the gate proves the line
     * ran. Idempotent per real player instance.
     */
    protected void markLineFired() {
        if (!lineFired) {
            lineFired = true;
            System.err.println("@@MARKER@@ name=" + getName());
        }
    }

@@MEMBERS@@
}
'''


def render_driver(deck: Deck, line_spec: LineSpec) -> str:
    """Stamp ``line_spec`` into the thin-driver template → a compilable ``Driver.java``.

    Returns the full Java source for ``deck``'s driver: the seam ctor + copy ctor +
    ``markLineFired`` scaffold (from the template) wrapping ``line_spec.members`` (the
    owned line). The class is ``Driver`` in the deck's :func:`driver_package`, so the
    emitted FQCN is :func:`driver_fqcn` and the file the gate writes is ``Driver.java``.
    """
    imports = '\n'.join(line_spec.imports)
    return (
        _DRIVER_TEMPLATE.replace('@@PACKAGE@@', driver_package(deck))
        .replace('@@IMPORTS@@', imports)
        .replace('@@NAME@@', line_spec.name)
        .replace('@@MARKER@@', DRIVER_LINE_FIRED_MARKER)
        .replace('@@MEMBERS@@', line_spec.members)
    )


# --------------------------------------------------------------------------- #
# The canonical worked example: the spike's proven Mikaeus + Triskelion combo.  #
# Ported from GoldfishVariants$ThinMikaeusDriver (the dual-context thin driver), #
# with the lab statics replaced by the standalone markLineFired() marker.       #
# Line: with Mikaeus (the Unhallowed) + Triskelion both on my battlefield, on my #
# main with an empty stack, ping the opponent with Triskelion's counters; at 2   #
# counters left, self-ping so Triskelion re-dies at 0 → undying refuels to 4 →   #
# loop to lethal. OWNS: this priority loop, Triskelion's damage target (face vs   #
# self-ping), and a 2-5-land mulligan. DEFERS all else (incl. ALL combat) to CP7. #
# --------------------------------------------------------------------------- #

_MIKAEUS_MEMBERS = '''\
    // set for the priority pass in which the ping should hit TRISKELION ITSELF
    private boolean pingSelfNext = false;

    // ---- OWNED #3: mulligan-for-the-line (land window only; visible info) ----
    @Override
    public boolean chooseMulligan(mage.game.Game game) {
        int handSize = hand.size();
        if (handSize <= 5) {
            return false; // never mulligan below 5 for a solo race
        }
        int lands = 0;
        for (mage.cards.Card c : hand.getCards(game)) {
            if (c.isLand(game)) {
                lands++;
            }
        }
        return lands < 2 || lands > 5; // ship no-landers and floods; keep the rest
    }

    private mage.game.permanent.Permanent findMine(mage.game.Game game, String name) {
        for (mage.game.permanent.Permanent p : game.getBattlefield().getAllActivePermanents(getId())) {
            if (p.getName().equals(name)) {
                return p;
            }
        }
        return null;
    }

    // ---- OWNED #1: the loop, ONLY when assembled on my main w/ empty stack ----
    @Override
    public boolean priority(mage.game.Game game) {
        boolean myPriority = getId().equals(game.getState().getPriorityPlayerId());
        boolean myMainStep = game.getTurnStepType() == mage.constants.PhaseStep.PRECOMBAT_MAIN
                || game.getTurnStepType() == mage.constants.PhaseStep.POSTCOMBAT_MAIN;
        if (!(myPriority && myMainStep && game.getStack().isEmpty())) {
            return super.priority(game); // everything else -> pure CP7
        }
        mage.game.permanent.Permanent mikaeus = findMine(game, "Mikaeus, the Unhallowed");
        mage.game.permanent.Permanent trisk = findMine(game, "Triskelion");
        if (mikaeus != null && trisk != null) {
            int counters = trisk.getCounters(game).getCount(mage.counters.CounterType.P1P1);
            mage.abilities.ActivatedAbility ping = null;
            for (mage.abilities.ActivatedAbility a : getPlayable(game, true)) {
                if (!a.getSourceId().equals(trisk.getId())) {
                    continue;
                }
                String rule = a.getRule() == null ? "" : a.getRule();
                if (rule.startsWith("Remove a")) {
                    ping = a;
                    break;
                }
            }
            if (ping != null && counters > 0) {
                // face while >2 counters; the last 2 go to self-pings so Triskelion
                // re-dies at 0 counters and undying refuels it to 4
                pingSelfNext = counters <= 2;
                boolean ok = activateAbility(ping, game);
                if (ok) {
                    markLineFired(); // the owned line is executing -> emit the gate marker
                    return false;
                }
            }
            // ping momentarily unavailable (mid death/undying return) -> let CP7
            // advance triggers/SBAs; we get priority back and resume the loop
            return super.priority(game);
        }
        // not assembled -> pure CP7 develops, casts the pieces, fights
        return super.priority(game);
    }

    // ---- OWNED #2: steer ONLY Triskelion's damage target (face vs self-ping) ----
    @Override
    public boolean chooseTarget(mage.constants.Outcome outcome, mage.target.Target target,
            mage.abilities.Ability source, mage.game.Game game) {
        if (outcome == mage.constants.Outcome.Damage
                && target instanceof mage.target.common.TargetAnyTarget && source != null) {
            mage.game.permanent.Permanent src = game.getPermanent(source.getSourceId());
            if (src != null && src.getName().equals("Triskelion") && src.isControlledBy(getId())) {
                if (pingSelfNext) {
                    mage.game.permanent.Permanent trisk = findMine(game, "Triskelion");
                    if (trisk != null && target.canTarget(getId(), trisk.getId(), source, game)) {
                        target.addTarget(trisk.getId(), source, game);
                        return true;
                    }
                }
                for (java.util.UUID opp : game.getOpponents(getId())) {
                    if (target.canTarget(getId(), opp, source, game)) {
                        target.addTarget(opp, source, game);
                        return true;
                    }
                }
            }
        }
        return super.chooseTarget(outcome, target, source, game);
    }
    // NOTE: no selectAttackers, no chooseUse, no develop override -- CP7 owns them.\
'''

#: The canonical Mikaeus + Triskelion fill -- the positive control the gate MUST pass.
#: Fully-qualified type references in the members keep the import surface minimal.
MIKAEUS_LINE_SPEC = LineSpec(
    name='mikaeus-triskelion-undying-loop',
    imports=(),
    members=_MIKAEUS_MEMBERS,
)
