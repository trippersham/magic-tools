/*
 * make-magic Forge sim-AI harness.
 *
 * Derived from Forge (forge.game.SimulateMatch, decompiled 2.0.13) and therefore
 * distributed under the GNU General Public License v3 (or later), matching both
 * upstream Forge and this repository. See the repo-root LICENSE.
 *
 * Copyright (C) 2011  Forge Team
 * Copyright (C) 2026  make-magic contributors
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 */
package org.makemagic.simai;

import com.google.common.eventbus.Subscribe;
import forge.GuiDesktop;
import forge.ai.AIOption;
import forge.deck.Deck;
import forge.deck.io.DeckSerializer;
import forge.game.Game;
import forge.game.GameEndReason;
import forge.game.GameLogEntry;
import forge.game.GameLogEntryType;
import forge.game.GameRules;
import forge.game.GameType;
import forge.game.Match;
import forge.game.card.Card;
import forge.game.event.GameEventSpellAbilityCast;
import forge.game.event.GameEventTurnBegan;
import forge.game.event.GameEventTurnEnded;
import forge.game.player.Player;
import forge.game.player.RegisteredPlayer;
import forge.game.zone.ZoneType;
import forge.gui.GuiBase;
import forge.model.FModel;
import forge.player.GamePlayerUtil;
import forge.view.TimeLimitedCodeBlock;
import org.apache.commons.lang3.time.StopWatch;

import java.io.File;
import java.lang.reflect.Field;
import java.util.ArrayList;
import java.util.Collections;
import java.util.EnumSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/**
 * Headless Forge match runner with the built-in simulation AI (AIOption.USE_SIMULATION)
 * optionally enabled — the hook the stock `sim` CLI verb does not expose.
 *
 * Usage:
 *   java -cp <patchclasses>:<forge-jar>:<this-dir> -Dapple.awt.UIElement=true SimAIMatch \
 *       -d /abs/deck1.dck /abs/deck2.dck [-n N] [-c clockSeconds] [-sim 0|1] \
 *       [-f constructed|commander] [-q] [-simdebug]
 *
 * -f       game format: `constructed` (default) or `commander`. Commander uses
 *          RegisteredPlayer.forCommander(deck) so the .dck [Commander] section
 *          populates the command zone (mirrors Forge's own SimulateMatch).
 * -sim 1   enable USE_SIMULATION for both AI players (default 1)
 * -simdebug  flip SimulationController.DEBUG via reflection to print per-node
 *            lookahead traces on stderr (proof the sim AI is active)
 *
 * HANDLOG instrumentation: the real (non-copied) Game gets an event-bus
 * subscriber that prints, in real time, a "HANDLOG" line at every turn start,
 * at every turn END (for EVERY player's turn, incl. opponents'), and at every
 * spell/ability put on the stack (by either player), each with a snapshot of
 * player 1's (deckA's) hand, untapped-land count, TOTAL-land count, and the
 * opponent's creature count. The stock game log is only flushed after the
 * game ends, so the HANDLOG stream is deliberately self-sufficient for
 * decision-point analysis. Disable with -nohandlog.
 *
 * Why both untapped AND total lands, and why an end-of-turn snapshot:
 * GameEventTurnBegan fires PRE-untap, so the turnstart `UR_untapped_lands`
 * count reflects the leftover-tapped state from prior turns, NOT the mana the
 * candidate will actually have this turn (all its lands untap moments later).
 * Reading affordability off that count SYSTEMATICALLY UNDERCOUNTS the
 * candidate's own-turn mana (it read 0 for whole games). `UR_total_lands` is
 * untap-invariant — every land untaps at the candidate's untap step, so the
 * post-untap available mana on its own turn is `UR_total_lands` (+ a land
 * drop), which the parser uses instead of the pre-untap untapped count. The
 * `event=turnend` snapshot captures the tapped state at the END of each turn
 * (tapped = total - untapped = the mana the active player SPENT that turn), so
 * "did the candidate leave mana up rather than interact?" is derivable.
 *
 * Must run with cwd = the Forge install dir (so res/ resolves), and with
 * -Dapple.awt.UIElement=true (NOT java.awt.headless) on macOS.
 */
public class SimAIMatch {
    static boolean handlog = true;

    public static void main(String[] args) throws Exception {
        System.setProperty("java.util.Arrays.useLegacyMergeSort", "true");
        GuiBase.setInterface(new GuiDesktop());
        FModel.initialize(null, null);

        List<String> decks = new ArrayList<>();
        int nGames = 1;
        int clock = 120;
        boolean useSim = true;
        boolean outputGamelog = true;
        boolean simDebug = false;
        GameType format = GameType.Constructed;

        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "-d":
                    while (i + 1 < args.length && !args[i + 1].startsWith("-")) {
                        decks.add(args[++i]);
                    }
                    break;
                case "-n": nGames = Integer.parseInt(args[++i]); break;
                case "-c": clock = Integer.parseInt(args[++i]); break;
                case "-sim": useSim = !"0".equals(args[++i]); break;
                case "-f": {
                    String f = args[++i];
                    if ("commander".equalsIgnoreCase(f)) {
                        format = GameType.Commander;
                    } else if ("constructed".equalsIgnoreCase(f)) {
                        format = GameType.Constructed;
                    } else {
                        System.err.println("Unknown -f format (expected constructed|commander): " + f);
                        System.exit(2);
                    }
                    break;
                }
                case "-q": outputGamelog = false; break;
                case "-simdebug": simDebug = true; break;
                case "-nohandlog": handlog = false; break;
                default:
                    System.err.println("Unknown arg: " + args[i]);
                    System.exit(2);
            }
        }
        if (decks.size() != 2) {
            System.err.println("Need exactly two -d deck files (absolute .dck paths)");
            System.exit(2);
        }

        if (simDebug) {
            Class<?> sc = Class.forName("forge.ai.simulation.SimulationController");
            Field dbg = sc.getDeclaredField("DEBUG");
            dbg.setAccessible(true);
            dbg.setBoolean(null, true);
            System.out.println("SimulationController.DEBUG enabled via reflection");
        }

        // Mirror Forge's own SimulateMatch: GameRules(type) + applied variant, and
        // RegisteredPlayer.forCommander(d) for the Commander game type (which reads
        // the .dck [Commander] section and sets up the command zone). Constructed is
        // the plain RegisteredPlayer(d) path.
        GameRules rules = new GameRules(format);
        rules.setAppliedVariants(EnumSet.of(format));
        rules.setSimTimeout(clock);

        Set<AIOption> opts = useSim ? EnumSet.of(AIOption.USE_SIMULATION) : null;
        List<RegisteredPlayer> pp = new ArrayList<>();
        int idx = 1;
        for (String path : decks) {
            Deck d = DeckSerializer.fromFile(new File(path));
            if (d == null) {
                System.err.println("Could not load deck - " + path);
                System.exit(2);
            }
            String name = "Ai(" + idx + ")-" + d.getName();
            RegisteredPlayer rp = format.equals(GameType.Commander)
                    ? RegisteredPlayer.forCommander(d)
                    : new RegisteredPlayer(d);
            rp.setPlayer(GamePlayerUtil.createAiPlayer(name, idx - 1, 0, opts));
            pp.add(rp);
            idx++;
        }

        System.out.println("SimAIMatch: " + decks.get(0) + " vs " + decks.get(1)
                + " | games=" + nGames + " clock=" + clock + "s format=" + format
                + " useSimulationAI=" + useSim);

        Match mc = new Match(rules, pp, "SimAIMatch");
        for (int g = 0; g < nGames; g++) {
            long t0 = System.currentTimeMillis();
            simulateSingleMatch(mc, g, outputGamelog);
            System.out.println("SimAIMatch: game " + (g + 1) + " wallclock " + (System.currentTimeMillis() - t0) + " ms");
        }
        System.out.flush();
        System.exit(0);
    }

    /** Clone of SimulateMatch.simulateSingleMatch (decompiled 2.0.13) with the
     *  HANDLOG subscriber registered between createGame() and startGame(). */
    static void simulateSingleMatch(Match mc, int iGame, boolean outputGamelog) {
        StopWatch sw = new StopWatch();
        sw.start();
        Game g1 = mc.createGame();
        HandLogger hl = handlog ? new HandLogger(g1) : null;
        if (hl != null) {
            g1.subscribeToEvents(hl);
        }
        try {
            TimeLimitedCodeBlock.runWithTimeout(() -> {
                mc.startGame(g1);
                sw.stop();
            }, mc.getRules().getSimTimeout(), TimeUnit.SECONDS);
        } catch (TimeoutException e) {
            System.out.println("Stopping slow match as draw");
        } catch (Exception | StackOverflowError e) {
            e.printStackTrace();
        } finally {
            if (sw.isStarted()) {
                sw.stop();
            }
            g1.setGameOver(GameEndReason.Draw);
        }
        if (hl != null) {
            hl.logFinal();
        }
        List<GameLogEntry> log = outputGamelog
                ? g1.getGameLog().getLogEntries(null)
                : g1.getGameLog().getLogEntries(GameLogEntryType.MATCH_RESULTS);
        Collections.reverse(log);
        for (GameLogEntry l : log) {
            System.out.println(l);
        }
        if (g1.getOutcome().isDraw()) {
            System.out.printf("%nGame Result: Game %d ended in a Draw! Took %d ms.%n", 1 + iGame, sw.getTime());
        } else {
            System.out.printf("%nGame Result: Game %d ended in %d ms. %s has won!%n%n", 1 + iGame, sw.getTime(),
                    g1.getOutcome().getWinningLobbyPlayer().getName());
        }
    }

    /** Real-time decision-point logger for player 1 (deckA, "Ai(1)-..."). */
    public static class HandLogger {
        private final Game game;
        private Player p1;

        HandLogger(Game game) {
            this.game = game;
        }

        private Player p1() {
            if (p1 == null) {
                for (Player p : game.getPlayers()) {
                    if (p.getName().startsWith("Ai(1)")) {
                        p1 = p;
                    }
                }
            }
            return p1;
        }

        private String snapshot() {
            Player p = p1();
            if (p == null) {
                return "UR_hand=[] UR_untapped_lands=0 UR_total_lands=0 opp_creatures=0 UR_life=0 opp_life=0";
            }
            List<String> hand = new ArrayList<>();
            for (Card c : p.getCardsIn(ZoneType.Hand)) {
                hand.add(c.getName());
            }
            int untapped = 0;
            int totalLands = 0;
            for (Card c : p.getLandsInPlay()) {
                totalLands++;
                if (!c.isTapped()) {
                    untapped++;
                }
            }
            int oppCreatures = 0;
            int oppLife = 0;
            for (Player o : game.getPlayers()) {
                if (o != p) {
                    oppCreatures += o.getCreaturesInPlay().size();
                    oppLife = o.getLife();
                }
            }
            return "UR_hand=[" + String.join(";", hand) + "] UR_untapped_lands=" + untapped
                    + " UR_total_lands=" + totalLands
                    + " opp_creatures=" + oppCreatures + " UR_life=" + p.getLife() + " opp_life=" + oppLife;
        }

        @Subscribe
        public void onTurn(GameEventTurnBegan ev) {
            System.out.println("HANDLOG turn=" + ev.turnNumber() + " event=turnstart active=" + ev.turnOwner()
                    + " " + snapshot());
        }

        // GameEventTurnEnded is a no-arg record (carries no turn/owner), so the
        // ending turn's number and active player are read from the phase handler,
        // which still points at the turn being closed out. Emitted for EVERY
        // player's turn (incl. opponents') per the spent-mana design: the snapshot
        // taken here is post-spend, so tapped = total - untapped = mana SPENT this
        // turn by the active player (all lands were untapped at its untap step).
        @Subscribe
        public void onTurnEnd(GameEventTurnEnded ev) {
            Player active = game.getPhaseHandler().getPlayerTurn();
            String owner = active != null ? active.getName() : "?";
            System.out.println("HANDLOG turn=" + game.getPhaseHandler().getTurn() + " event=turnend active=" + owner
                    + " " + snapshot());
        }

        @Subscribe
        public void onCast(GameEventSpellAbilityCast ev) {
            String kind = ev.sa().isSpell() ? "spell" : "ability";
            System.out.println("HANDLOG turn=" + game.getPhaseHandler().getTurn() + " event=cast kind=" + kind
                    + " castBy=" + ev.si().getActivatingPlayer() + " source=" + ev.si().getSourceCard().getName()
                    + " " + snapshot());
        }

        void logFinal() {
            System.out.println("HANDLOG turn=" + game.getPhaseHandler().getTurn() + " event=gameend " + snapshot());
        }
    }
}
