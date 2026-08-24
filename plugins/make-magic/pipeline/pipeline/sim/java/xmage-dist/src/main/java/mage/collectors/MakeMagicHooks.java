package mage.collectors;

import mage.cards.Card;
import mage.collectors.services.EmptyDataCollector;
import mage.game.Game;
import mage.game.permanent.Permanent;
import mage.players.Player;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * make-magic Phase-2 telemetry hook (task 2.2).
 *
 * Lives in package {@code mage.collectors} for package access to
 * {@code DataCollectorServices.activeServices} (no public register API). It
 * translates the real XMage game into the SAME line contract the Forge sim-AI
 * harness emits, so the whole (M1/M2/M3-hardened) Forge telemetry parser
 * ({@code extract_piloting} + {@code extract_game_features}) consumes XMage logs
 * verbatim — XMage becomes a drop-in with identical PilotingProfile + GameFeatures.
 *
 * Slot mapping mirrors Forge: candidate (deck A / PlayerA) = {@code Ai(1)},
 * opponent (PlayerB) = {@code Ai(2)}. The {@code UR_*} HANDLOG fields always report
 * the candidate's private state.
 *
 * Emitted per real game (AI simulation sub-games skipped via {@code isSimulation()}):
 *   Turn:   "Turn: Turn N (Ai(k)-Player)"                 (turn counter + ramp boundaries)
 *   Land:   "Land: Ai(k)-Player played a land"            (ramp curve; state-tracked)
 *   Life:   "Life: Life: Ai(k)-Player <old> > <new>"      (kill-turn + win margin; state-tracked)
 *   Damage: "Damage: <src> deals N [combat ]damage to Ai(k)-Player."  (wincon combat/burn)
 *   HANDLOG turn=N event=turnstart|cast|gameend ...       (piloting opportunity model)
 *   (Game Result: line is emitted by XMageBatch.main after start(), where hasWon() is set.)
 */
public class MakeMagicHooks extends EmptyDataCollector {

    private static final Pattern HTML = Pattern.compile("<[^>]*>");
    private static final Pattern CAST =
            Pattern.compile("^(Player[AB]) casts (.+?)(?: \\[[0-9a-f]+\\]| targeting | from )");
    // Damage to a PLAYER surfaces as a life-loss message that carries XMage's OWN
    // combat flag + the real source (PlayerImpl.loseLife): e.g.
    //   "PlayerA loses 4 life at combat from Goblin Guide"  -> combat, src=Goblin Guide
    //   "PlayerA loses 3 life from Lava Spike"              -> non-combat (burn), src=Lava Spike
    // This is the authoritative combat/burn classification — no step-heuristic guess.
    private static final Pattern LOSES =
            Pattern.compile("^(Player[AB]) loses (\\d+) life( at combat)?(?: from (.+?))?\\.?$");

    private UUID candidateId;
    private int lastTurn = -1;
    private boolean opened = false;
    private final Map<UUID, Integer> lastLife = new HashMap<>();
    private final Map<UUID, Integer> lastLands = new HashMap<>();

    public static void install() {
        DataCollectorServices.getInstance().activeServices.add(new MakeMagicHooks());
    }

    @Override
    public String getServiceCode() {
        return "makeMagicHooks";
    }

    @Override
    public String getInitInfo() {
        return "make-magic HANDLOG + GameFeatures emitter (Forge-compatible telemetry)";
    }

    @Override
    public void onGameStart(Game game) {
        lastTurn = -1;
        opened = false;
        candidateId = null;
        lastLife.clear();
        lastLands.clear();
        for (Player p : game.getPlayers().values()) {
            if (p.getName().equals("PlayerA")) {
                candidateId = p.getId();
            }
            lastLife.put(p.getId(), p.getLife());
            lastLands.put(p.getId(), 0);
        }
    }

    @Override
    public void onGameLog(Game game, String message) {
        if (candidateId == null) {
            return;
        }
        String plain = HTML.matcher(message).replaceAll("").trim();

        // Opening-hand log line (human-readable; also proves testMode=false).
        if (!opened) {
            Player cand = game.getPlayer(candidateId);
            if (cand != null && !cand.getHand().isEmpty()) {
                opened = true;
                for (Player p : game.getPlayers().values()) {
                    System.out.println("XMAGEBATCH opening_hand " + p.getName()
                            + " (" + p.getHand().size() + "): [" + handNames(p, game) + "]");
                }
            }
        }

        int turn = game.getTurnNum();
        // Turn boundary -> Forge "Turn:" line (game-features) + a turnstart HANDLOG.
        if (turn != lastTurn) {
            lastTurn = turn;
            Player active = game.getPlayer(game.getActivePlayerId());
            System.out.println("Turn: Turn " + turn + " (" + slot(active) + ")");
            System.out.println("HANDLOG turn=" + turn + " event=turnstart active=" + slot(active)
                    + " " + snapshot(game));
        }

        // A spell/ability cast -> cast HANDLOG (the parser decides counter/removal
        // opportunities from the candidate deck's buckets).
        Matcher m = CAST.matcher(plain);
        if (m.find()) {
            Player caster = playerByName(game, m.group(1));
            if (caster != null) {
                System.out.println("HANDLOG turn=" + turn + " event=cast kind=spell castBy=" + slot(caster)
                        + " source=" + m.group(2).trim() + " " + snapshot(game));
            }
        }

        // Life-loss to a player -> Forge "Damage:" line carrying XMage's authoritative
        // combat flag ("at combat") + real source. Emitted BEFORE the Life: line so a
        // lethal blow is classified before the parser sees life <= 0.
        Matcher lo = LOSES.matcher(plain);
        if (lo.find()) {
            Player target = playerByName(game, lo.group(1));
            if (target != null) {
                boolean combat = lo.group(3) != null;
                String src = lo.group(4) != null ? lo.group(4).trim() : "source";
                System.out.println("Damage: " + src + " deals " + lo.group(2)
                        + (combat ? " combat" : "") + " damage to " + slot(target) + ".");
            }
        }

        // State-tracked life + land changes (reliable, message-independent).
        emitLifeChanges(game);
        emitLandChanges(game);
    }

    @Override
    public void onGameEnd(Game game) {
        // gameend HANDLOG (stranded-interaction reads its UR_hand) while state intact.
        // The "Game Result:" terminator is emitted by XMageBatch.main after start()
        // (hasWon() is not reliably set here).
        System.out.println("HANDLOG turn=" + game.getTurnNum() + " event=gameend " + snapshot(game));
    }

    // --- game-features emitters ------------------------------------------- //

    private void emitLifeChanges(Game game) {
        for (Player p : game.getPlayers().values()) {
            int life = p.getLife();
            int prev = lastLife.getOrDefault(p.getId(), life);
            if (life != prev) {
                // Forge format: "Life: Life: Ai(k)-Name <old> > <new>" (old is non-negative).
                // The combat/burn Damage: line is emitted from the "loses N life at
                // combat from X" message above (XMage's own flag), not inferred here.
                if (prev >= 0) {
                    System.out.println("Life: Life: " + slot(p) + " " + prev + " > " + life);
                }
                lastLife.put(p.getId(), life);
            }
        }
    }

    private void emitLandChanges(Game game) {
        for (Player p : game.getPlayers().values()) {
            int lands = 0;
            for (Permanent perm : game.getBattlefield().getAllActivePermanents(p.getId())) {
                if (perm.isLand()) {
                    lands++;
                }
            }
            int prev = lastLands.getOrDefault(p.getId(), 0);
            for (int i = prev; i < lands; i++) {
                System.out.println("Land: " + slot(p) + " played a land");
            }
            if (lands != prev) {
                lastLands.put(p.getId(), lands);
            }
        }
    }

    // --- helpers ---------------------------------------------------------- //

    /** ``Ai(1)-Name`` for the candidate (PlayerA), ``Ai(2)-Name`` otherwise. */
    private String slot(Player player) {
        if (player == null) {
            return "Ai(2)-?";
        }
        return (player.getId().equals(candidateId) ? "Ai(1)-" : "Ai(2)-") + player.getName();
    }

    private Player playerByName(Game game, String name) {
        for (Player p : game.getPlayers().values()) {
            if (p.getName().equals(name)) {
                return p;
            }
        }
        return null;
    }

    private String handNames(Player player, Game game) {
        List<String> names = new ArrayList<>();
        for (Card c : player.getHand().getCards(game)) {
            names.add(c.getName());
        }
        return String.join(";", names);
    }

    /** Candidate-centric UR_* snapshot: hand / untapped lands / total lands +
     *  the opponent's creature count + both life totals. */
    private String snapshot(Game game) {
        Player cand = game.getPlayer(candidateId);
        if (cand == null) {
            return "UR_hand=[] UR_untapped_lands=0 UR_total_lands=0 opp_creatures=0 UR_life=0 opp_life=0";
        }
        int untapped = 0;
        int totalLands = 0;
        for (Permanent p : game.getBattlefield().getAllActivePermanents(candidateId)) {
            if (p.isLand()) {
                totalLands++;
                if (!p.isTapped()) {
                    untapped++;
                }
            }
        }
        int oppCreatures = 0;
        int oppLife = 0;
        for (Player o : game.getPlayers().values()) {
            if (!o.getId().equals(candidateId)) {
                for (Permanent p : game.getBattlefield().getAllActivePermanents(o.getId())) {
                    if (p.isCreature(game)) {
                        oppCreatures++;
                    }
                }
                oppLife = o.getLife();
            }
        }
        return "UR_hand=[" + handNames(cand, game) + "] UR_untapped_lands=" + untapped
                + " UR_total_lands=" + totalLands
                + " opp_creatures=" + oppCreatures + " UR_life=" + cand.getLife() + " opp_life=" + oppLife;
    }
}
