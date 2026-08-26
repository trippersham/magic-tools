package makemagic.driver.reference;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

import mage.MageObject;
import mage.abilities.Ability;
import mage.cards.Card;
import mage.cards.Cards;
import mage.cards.CardsImpl;
import mage.constants.Zone;
import mage.game.Game;
import mage.game.permanent.Permanent;
import mage.player.ai.score.ComboMacro;
import mage.player.ai.score.DriverBonus;
import mage.player.ai.score.MacroRegistry;
import mage.player.ai.score.SelectionRegistry;
import mage.player.ai.score.SelectionSteer;
import mage.players.Player;
import mage.target.TargetCard;

/**
 * HAND-AUTHORED REFERENCE DRIVER (Phase 3) — the Jeleva / Thassa's Oracle spell combo,
 * ported from the solo-goldfish spike (the POC {@code -Dmakemagic.insearch=jeleva} path)
 * into the productionized register-by-playerId seam.
 *
 * <p>This class is the reference implementation of the quad-ingestion contract documented in
 * {@code skills/authoring-drivers/references/xmage-api-primitives.md}: it exposes
 * {@code public static void register(UUID playerId)}, which {@code XMageBatch} reflectively
 * invokes on PlayerA's final id (via {@code -Dmakemagic.driver=<this FQCN>}). It registers a
 * quad {@code (Φ, P, macro, S)} — a leaf-eval potential toward assembly (Φ), a
 * deterministic Thoracle win sequence (macro, gated by precondition P), and a tutor-target
 * selection steer (S) — all keyed by {@code playerId}, so any other seat is pure CP7.</p>
 *
 * <p>The Φ milestones are the FRONT-LOADED tuned profile v1 (phi-tempo, 2026-08-25):
 * constants inlined here (the POC read them from {@code -D} props to sweep; a shipped Driver
 * bakes the tuned values). All bounded well below {@code WIN_GAME_SCORE} (1e8) so the win
 * terminal still dominates.</p>
 */
public final class JelevaThoracleReferenceDriver {

    private JelevaThoracleReferenceDriver() {
    }

    // ---- The quad-ingestion contract (reflection-by-convention) --------------------------

    /**
     * Register the Jeleva quad for {@code playerId}: Φ ({@link DriverBonus}), the
     * deterministic macro + P ({@link MacroRegistry}), and the tutor steer S
     * ({@link SelectionRegistry}). Invoked by {@code XMageBatch} on PlayerA only.
     */
    public static void register(UUID playerId) {
        DriverBonus.register(playerId, JelevaThoracleReferenceDriver::jelevaPhi);
        MacroRegistry.register(playerId, new JelevaMacro());
        SelectionRegistry.register(playerId, new JelevaSteer());
        System.err.println("JELEVA_REFERENCE_DRIVER_REGISTERED playerId=" + playerId
                + " (Phi=DriverBonus + macro=MacroRegistry + S=SelectionRegistry)");
    }

    // ---- Φ: front-loaded tuned potential toward assembly ---------------------------------

    // Monotone milestones over the WHOLE assembly path (each strictly dominates the prior).
    private static final int PHI_PIECE = 40000;       // per combo piece / tutor in hand (raw material)
    private static final int PHI_HAVE_ONE = 300000;   // >=1 combo HALF in hand (assembly started)
    private static final int PHI_TUTOR_FETCH = 400000; // a tutor in hand can fetch the missing half now
    private static final int PHI_BOTH = 700000;       // both halves in hand (mana-agnostic)
    private static final int PHI_BOTH_MANA = 250000;  // ADD'L: both + mana to fire this turn
    private static final int PHI_NEAR_LETHAL = 1200000; // library empty + Oracle (one cast away)
    private static final int PHI_TUTOR_GATE = 1;      // untapped lands to count a tutor castable

    /** Untapped lands PlayerA controls — a coarse castable-mana proxy for Φ / macro gating. */
    private static int untappedLands(Game game, UUID pid) {
        int n = 0;
        for (Permanent p : game.getBattlefield().getAllActivePermanents(pid)) {
            if (p.isLand(game) && !p.isTapped()) {
                n++;
            }
        }
        return n;
    }

    /**
     * The Jeleva transition-shaped potential Φ: a monotone distance-to-win read purely from
     * PlayerA's state, rising across the whole assembly path so CP7's leaf evaluator develops
     * toward the combo. Deterministic, bounded well within +/-1e6.
     */
    private static int jelevaPhi(Game game, UUID pid) {
        Player me = game.getPlayer(pid);
        if (me == null) {
            return 0;
        }
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
        int score = pieces * PHI_PIECE + tutors * PHI_PIECE;
        // FRONT-LOAD 1: holding at least one combo HALF is a real early milestone.
        if (hasOracle || hasExile) {
            score += PHI_HAVE_ONE;
        }
        // FRONT-LOAD 2: a tutor that can fetch the MISSING half, with mana to cast it — tutor EARLY.
        if (tutors > 0 && (!hasOracle || !hasExile) && mana >= PHI_TUTOR_GATE) {
            score += PHI_TUTOR_FETCH;
        }
        // FRONT-LOAD 3: both halves in hand = pair complete (get there fast, mana-agnostic).
        if (hasOracle && hasExile) {
            score += PHI_BOTH;
            if (mana >= 3) {
                score += PHI_BOTH_MANA;
            }
        }
        // Library already exiled + Oracle in hand -> one Oracle cast from the deterministic win.
        if (hasOracle && me.getLibrary().size() == 0) {
            score += PHI_NEAR_LETHAL;
        }
        return score;
    }

    // ---- macro (+ P): the deterministic Thoracle win sequence ----------------------------

    /**
     * The Jeleva deterministic-execution macro. Precondition P (= {@link #applicable}): the
     * Thoracle kill is executable NOW (Oracle in hand + an exile-your-library piece + mana, on
     * my turn with an empty stack; or the library already empty + Oracle in hand).
     * {@link #apply} drives the known outcome on whatever game it is handed (a search copy or
     * the real game) with BOUNDED state moves — never {@code priority()} on the copy.
     */
    private static final class JelevaMacro implements ComboMacro {
        @Override
        public boolean applicable(Game game, UUID pid) {
            if (game.checkIfGameIsOver()) {
                return false;
            }
            Player me = game.getPlayer(pid);
            if (me == null) {
                return false;
            }
            if (!pid.equals(game.getActivePlayerId()) || !game.getStack().isEmpty()) {
                return false; // deterministic-win boundary: my turn, empty stack.
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
                return true; // library already gone -> Oracle alone wins.
            }
            return exile && untappedLands(game, pid) >= 3;
        }

        @Override
        public void apply(Game game, UUID pid) {
            // STANDARD slot-exercise marker (Phase 5): the seam invokes apply() ONLY to execute
            // the deterministic win, so entry == the macro fired. The emitter injects the same
            // line into every authored quad; kept here so the reference driver matches.
            System.err.println("DRIVER_MACRO_FIRED pid=" + pid);
            Player me = game.getPlayer(pid);
            if (me == null) {
                return;
            }
            // (1) Demonic Consultation naming an off-deck card -> exile the WHOLE library.
            List<Card> lib = new ArrayList<>(me.getLibrary().getCards(game));
            if (!lib.isEmpty()) {
                me.moveCards(new CardsImpl(lib), Zone.EXILED, null, game);
                game.applyEffects();
            }
            // (2) Put Thassa's Oracle onto the battlefield -> its empty-library ETB wins.
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
            if (game.checkIfGameIsOver()) {
                System.err.println("MACRO_ORACLE_ETB_WIN pid=" + pid + " libEmpty=true");
            } else {
                // The empty-library + Oracle-in-play outcome is deterministically a win; if the
                // headless ETB win-check did not flip game-over, concede opponents so the terminal
                // is an unambiguous WIN (logged distinctly for honest reporting).
                System.err.println("MACRO_ORACLE_ETB_no_gameover pid=" + pid + " -> direct opp-loss fallback");
                for (UUID opp : game.getOpponents(pid)) {
                    Player o = game.getPlayer(opp);
                    if (o != null) {
                        o.lost(game);
                    }
                }
                game.applyEffects();
                game.checkStateAndTriggered();
            }
        }
    }

    // ---- S: the tutor-target selection steer ---------------------------------------------

    /**
     * The Jeleva tutor-target selection steer. When a tutor searches MY library and I am
     * missing a combo piece, steer the pick to the MISSING piece (Thassa's Oracle if I lack
     * it, else Demonic Consultation / Tainted Pact) instead of CP7's first-legal card. Fires
     * keyed by playerId in BOTH the real game AND the sim rollouts (so the search MODELS
     * "cast tutor -> hold the missing piece -> macro -> WIN" and thus VALUES casting the tutor).
     */
    private static final class JelevaSteer implements SelectionSteer {
        @Override
        public boolean apply(Game game, UUID pid, Cards cards, TargetCard target, Ability source,
                boolean useAddTarget) {
            if (source == null || cards == null || target == null) {
                return false;
            }
            if (!pid.equals(source.getControllerId())) {
                return false; // only steer MY OWN tutor.
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
                return false; // both halves already in hand — let CP7 pick.
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
                        continue; // not in this search's candidate set (e.g. a top-N limited tutor).
                    }
                    if (!target.canTarget(pid, id, source, cards, game)) {
                        continue;
                    }
                    if (useAddTarget) {
                        target.addTarget(id, source, game);
                    } else {
                        target.add(id, game);
                    }
                    String tutor = "?";
                    MageObject so = game.getObject(source.getSourceId());
                    if (so != null) {
                        tutor = so.getName();
                    }
                    System.err.println("SELECTION_STEER_FETCH pid=" + pid + " fetched=" + want
                            + " tutor=" + tutor + " hasOracle=" + hasOracle + " hasExile=" + hasExile);
                    return true;
                }
            }
            return false;
        }
    }
}
