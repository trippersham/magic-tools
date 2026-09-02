package org.makemagic.xmage;

import mage.abilities.Ability;
import mage.cards.decks.Deck;
import mage.cards.decks.DeckCardLists;
import mage.cards.decks.importer.DeckImporter;
import mage.cards.repository.CardScanner;
import mage.collectors.MakeMagicHooks;
import mage.constants.CommanderCardType;
import mage.constants.MultiplayerAttackOption;
import mage.constants.RangeOfInfluence;
import mage.counters.CounterType;
import mage.game.CommanderDuel;
import mage.game.FreeForAllMatch;
import mage.game.Game;
import mage.game.GameOptions;
import mage.game.TwoPlayerDuel;
import mage.game.match.Match;
import mage.game.match.MatchOptions;
import mage.game.mulligan.MulliganType;
import mage.player.ai.ComputerPlayer;
import mage.player.ai.ComputerPlayer7;
import mage.player.ai.score.DriverBonus;
import mage.player.ai.score.MacroRegistry;
import mage.player.ai.score.MulliganRegistry;
import mage.player.ai.score.SelectionRegistry;
import mage.players.Player;
import mage.watchers.common.CommanderInfoWatcher;

import com.google.gson.Gson;

import java.io.BufferedReader;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * Phase-2 SPIKE (task 2.1) + shared-per-deck-driver Phase 0: a STANDALONE headless
 * XMage game runner, OUTSIDE the JUnit/surefire scaffold.
 *
 * The gate the SPIKE proved: two raw production {@link ComputerPlayer7}s play a
 * {@link TwoPlayerDuel} to a decisive result with REAL 7-card opening hands +
 * mulligans (gameOptions.testMode=false — NOT the 0-card testMode path), driven
 * only by {@code game.start()} on the {@code main} thread (which XMage's
 * game-thread guard whitelists). Opening hands + the game log are surfaced by
 * {@link MakeMagicHooks}.
 *
 * Phase 3 productionizes the in-search quad DRIVER SEAM (supersedes the Phase-0
 * engine-replacement {@code -Dmakemagic.driverA} path):
 *   1. DRIVER SEAM (register-by-playerId) — a per-deck driver does NOT replace
 *      PlayerA's engine. PlayerA stays a plain {@link ComputerPlayer7}; the driver
 *      REGISTERS its quad {@code (Φ, P, macro, S)} by PlayerA's stable {@code playerId}
 *      into the dist's seam registries ({@code DriverBonus}/{@code MacroRegistry}/
 *      {@code SelectionRegistry}), which the patched minimax consults inside the search.
 *      Selected via {@code -Dmakemagic.driver=<FQCN>}: the FQCN is a classpath-injected,
 *      separately-compiled Driver class exposing
 *      {@code public static void register(java.util.UUID playerId)}, invoked reflectively
 *      by convention AFTER {@code game/match.addPlayer} (the id is then final). Only
 *      PlayerA is ever registered; PlayerB stays pure CP7. Registration failure is
 *      FAIL-LOUD (a bad Driver must not silently run as pure CP7 and mask a broken gate).
 *      Unset/empty ⇒ no registration ⇒ pure CP7 (byte-identical prior behavior).
 *   2. SOLO (goldfish) MODE — {@code <deckA> --solo [games] [skill] [commander]}:
 *      PlayerB is a do-nothing passer (passes every priority, never blocks); PlayerA
 *      is ALWAYS on the play. Measures the OWN-turn kill
 *      ({@code ownTurn = (globalTurn + 1) / 2}) and emits a
 *      {@code GOLDFISH SUMMARY (OWN TURNS) ...} line. Ported from the lab's
 *      {@code GoldfishBatchTest}. The optional 5th {@code commander} token makes the
 *      goldfish COMMANDER-NATIVE (a {@link CommanderDuel}, 40 life + command zone, vs a
 *      legal 100-card commander passer shell); absent it is the constructed 60-basics
 *      passer (byte-identical prior behavior).
 *
 * Usage:
 *   MATCH:  java -cp &lt;cp&gt; [-Dmakemagic.driver=&lt;FQCN&gt;] org.makemagic.xmage.XMageBatch &lt;deckA&gt; &lt;deckB&gt; [games] [skill] [commander]
 *   SOLO:   java -cp &lt;cp&gt; [-Dmakemagic.driver=&lt;FQCN&gt;] org.makemagic.xmage.XMageBatch &lt;deckA&gt; --solo [games] [skill] [commander]
 *   WARM:   java -cp &lt;cp&gt; org.makemagic.xmage.XMageBatch --warm
 * Run with cwd = a dir holding the H2 db/ (built by CardScanner.scan() on first use).
 */
public class XMageBatch {

    /**
     * {@code -Dmakemagic.driver=<FQCN>} — the in-search quad driver REGISTRATION seam
     * (PlayerA only). The FQCN names a classpath-injected Driver class exposing
     * {@code public static void register(java.util.UUID playerId)}; the id-keyed quad it
     * registers is consulted by the patched minimax. Unset ⇒ no registration ⇒ pure CP7.
     */
    private static final String DRIVER_PROP = "makemagic.driver";

    /**
     * PER-DECISION AI think-time budget (wall seconds), applied to every seated
     * {@link ComputerPlayer7} via {@code setMaxThinkTimeSecs}. XMage's real-play AI already
     * bounds each top-level decision through {@code ComputerPlayer6.addActionsTimed()} —
     * it runs the minimax in a {@code FutureTask} and does {@code future.get(maxThinkTimeSecs,
     * SECONDS)}, then on timeout interrupts the worker (the search polls
     * {@code Thread.isInterrupted()} and bails "AI game sim interrupted by timeout",
     * returning the best move found so far). The default is {@code skill * 3} (= 18s at
     * skill 6), which is fine for narrow boards but lets a SINGLE wide-board decision
     * (40+ permanents, many Treasures/tokens ⇒ huge branching) burn the full 18s, and a
     * wide-board turn grants priority dozens of times, so one legitimately-grinding TURN
     * blows the 300s Python stall watchdog WITHOUT advancing a turn. Capping the per-decision
     * budget lower bounds that pathological tail: each decision terminates with a rushed-but-
     * valid best move instead of stalling. This is a pure DELIBERATION-TIME guard — it changes
     * only how long the AI thinks, never the game rules, turn structure, or (beyond search-depth
     * noise) the outcome. Overridable via {@code -Dmakemagic.maxThinkSecs=<n>}; {@code <=0}
     * leaves XMage's default untouched.
     */
    private static final int MAX_THINK_TIME_SECS =
            Integer.getInteger("makemagic.maxThinkSecs", 3);

    /**
     * HARD per-game absolute wall-clock deadline (seconds). Unlike {@link #MAX_THINK_TIME_SECS}
     * (a per-AI-DECISION budget) and {@code setTurnCap} ({@code GameOptions.stopOnTurn}, checked
     * ONLY at the top of each turn's untap), this bounds the TOTAL wall time one call to
     * {@code game.start(...)} may consume — the ONLY cap that stops an INTRA-TURN livelock the AI
     * never breaks (e.g. two {@code Blood Celebrant} copies oscillating a player's life around 0
     * forever inside a single turn's state-based-action loop: never advances to the next untap, so
     * {@code stopOnTurn} never fires; mandatory SBA/damage events, so no think budget applies; and
     * {@code game.start()} never returns, so no terminal result is ever emitted). A lightweight
     * daemon watcher started per game forces the game to end once the budget elapses — by calling
     * {@code game.end()} AND interrupting the game thread, both of which XMage's inner play loops
     * observe via {@code checkIfGameIsOver()} (which returns true on {@code state.isGameOver()} OR
     * {@code Thread.currentThread().isInterrupted()} — the SANCTIONED third-party stop path). The
     * timed-out game is scored as a LOSS for the active (looping) player. Resolved once at startup
     * from {@code -Dmakemagic.maxGameWallclockSecs} (or env {@code MAKE_MAGIC_MAX_GAME_WALLCLOCK_SECS});
     * DEFAULT 150; {@code <= 0} disables the watcher (unbounded — the prior behavior).
     */
    private static final long MAX_GAME_WALLCLOCK_SECS = resolveGameWallclockSecs();

    /** Resolve the hard per-game deadline: sysprop, then env, then the 150s default. */
    private static long resolveGameWallclockSecs() {
        String raw = System.getProperty("makemagic.maxGameWallclockSecs");
        if (raw == null || raw.isEmpty()) {
            raw = System.getenv("MAKE_MAGIC_MAX_GAME_WALLCLOCK_SECS");
        }
        if (raw == null || raw.isEmpty()) {
            return 150L;
        }
        try {
            return Long.parseLong(raw.trim());
        } catch (NumberFormatException nfe) {
            return 150L; // a malformed budget falls back to the documented default.
        }
    }

    /**
     * HARD per-game transcript BYTE CAP (bytes) — a defense-in-depth DISK backstop on the per-game
     * transcript file (see {@link TranscriptCappingOutputStream}). The T2 wall-clock deadline
     * ({@link #MAX_GAME_WALLCLOCK_SECS}) bounds transcript growth INDIRECTLY (it ends the game at
     * ~budget), but there is no EXPLICIT ceiling on the FILE: a base livelock once wrote an
     * 84 MB / 1.7M-line transcript, and a slow-but-legal long game — or any future loop the
     * deadline is slow to catch — can still pressure disk. Once a game's transcript writes STRICTLY
     * exceed this many bytes, one truncation-marker line is written and further transcript bytes are
     * DISCARDED; the game keeps PLAYING to its RESULT unchanged (capping the log never ends the game
     * or alters the outcome). The counter resets PER GAME. Resolved once at startup from
     * {@code -Dmakemagic.maxTranscriptBytes} (or env {@code MAKE_MAGIC_MAX_TRANSCRIPT_BYTES});
     * DEFAULT 64 MiB — comfortably above any real game's transcript, small enough to bound a
     * pathological one. {@code <= 0} disables the cap (unbounded — the prior behavior; the stream is
     * left UNWRAPPED, byte-identical to before).
     */
    private static final long MAX_TRANSCRIPT_BYTES = resolveMaxTranscriptBytes();

    /** Resolve the per-game transcript byte cap: sysprop, then env, then the 64 MiB default. */
    private static long resolveMaxTranscriptBytes() {
        String raw = System.getProperty("makemagic.maxTranscriptBytes");
        if (raw == null || raw.isEmpty()) {
            raw = System.getenv("MAKE_MAGIC_MAX_TRANSCRIPT_BYTES");
        }
        if (raw == null || raw.isEmpty()) {
            return 64L * 1024 * 1024; // 64 MiB default.
        }
        try {
            return Long.parseLong(raw.trim());
        } catch (NumberFormatException nfe) {
            return 64L * 1024 * 1024; // a malformed budget falls back to the documented default.
        }
    }

    public static void main(String[] args) throws Exception {
        // Warm-up mode: build/verify the H2 card DB in a SINGLE process and exit.
        // The caller runs this ONCE, serialized, before launching the parallel game
        // JVMs — CardScanner.scan() is not concurrency-safe on a COLD build (parallel
        // cold scans race ExpansionRepository init → NPE / a corrupted 'BAD' db /
        // partial deck loads). After this returns the db is complete; concurrent
        // read-only scans in the game JVMs are then safe.
        if (args.length == 1 && "--warm".equals(args[0])) {
            MakeMagicHooks.install();
            System.out.println("XMAGEBATCH warming card database (single-process build)...");
            CardScanner.scan();
            System.out.println("XMAGEBATCH WARM card db ready.");
            System.out.flush();
            System.exit(0);
        }

        // PERSISTENT-WORKER mode: `XMageBatch --worker [--worker-max-games N]`. A long-lived
        // JVM that scans the card DB ONCE, then loops: print READY, read one `TASK {json}`
        // line from stdin, run ONE game (each seat's driver loaded per-game via a fresh
        // URLClassLoader, discarded after), print `RESULT {json}`, reset the seam registries,
        // loop. EOF on stdin → clean exit; after --worker-max-games games → clean exit (the
        // governor respawns). This SUPERSEDES the -Dmakemagic.driver-at-boot model for the
        // queue path; the --solo / match / --warm paths (below) keep the -D seam intact.
        if ("--worker".equals(args[0])) {
            int maxGames = 500;
            for (int i = 1; i + 1 < args.length; i++) {
                if ("--worker-max-games".equals(args[i])) {
                    try {
                        maxGames = Integer.parseInt(args[i + 1]);
                    } catch (NumberFormatException nfe) {
                        maxGames = 500; // a malformed count falls back to the documented default.
                    }
                }
            }
            runWorker(maxGames);
            return;
        }

        if (args.length < 1) {
            System.err.println("usage: XMageBatch <deckA> <deckB> [games] [skill] [commander]  |  "
                    + "XMageBatch <deckA> --solo [games] [skill] [commander]  |  XMageBatch --warm");
            System.exit(2);
        }

        // SOLO (goldfish) dispatch: `<deckA> --solo [games] [skill] [commander]`. The
        // `--solo` sentinel in the deckB position selects the do-nothing-opponent
        // goldfish harness. An optional 5th `commander` token (mirroring the MATCH 5th-arg
        // convention) selects the commander-native goldfish (CommanderDuel, 40 life +
        // command zone). Absent -> the constructed goldfish (byte-identical prior argv).
        if (args.length >= 2 && "--solo".equals(args[1])) {
            String deckAPath = args[0];
            int games = args.length > 2 ? Integer.parseInt(args[2]) : 1;
            int skill = args.length > 3 ? Integer.parseInt(args[3]) : 6;
            boolean commander = args.length > 4 && "commander".equalsIgnoreCase(args[4]);
            runSolo(deckAPath, games, skill, commander);
            return;
        }

        if (args.length < 2) {
            System.err.println("usage: XMageBatch <deckA> <deckB> [games] [skill] [commander]  |  "
                    + "XMageBatch <deckA> --solo [games] [skill] [commander]  |  XMageBatch --warm");
            System.exit(2);
        }
        String deckAPath = args[0];
        String deckBPath = args[1];
        int games = args.length > 2 ? Integer.parseInt(args[2]) : 1;
        int skill = args.length > 3 ? Integer.parseInt(args[3]) : 6;
        // Optional 5th positional token selects 1v1 Commander (EDH). The Python engine
        // appends 'commander' for the commander format; constructed sends only 4 args.
        boolean commander = args.length > 4 && "commander".equalsIgnoreCase(args[4]);
        // Commander seats each player at RangeOfInfluence.ALL (multiplayer influence),
        // constructed keeps the ONE range that TwoPlayerDuel uses.
        RangeOfInfluence range = commander ? RangeOfInfluence.ALL : RangeOfInfluence.ONE;

        MakeMagicHooks.install();
        System.out.println("XMAGEBATCH scanning card database (first run builds it)...");
        CardScanner.scan();
        System.out.println("XMAGEBATCH card db ready; deckA=" + deckAPath + " deckB=" + deckBPath
                + " games=" + games + " skill=" + skill + " commander=" + commander);

        // Per-game GLOBAL turn cap (see setTurnCap): bound a non-closing match so it STOPS
        // instead of grinding to deckout (~90+ turns). maxTurn is the own-turn budget (25
        // commander / 20 constructed, matching the solo goldfish sentinel); the global cap is
        // 2*maxTurn (both seats play), stamped via GameOptions.stopOnTurn. A game that reaches
        // the cap undecided ends with winnerId=null -> "DRAW/UNFINISHED" (an undecided game the
        // win-rate lens simply excludes), never a fabricated winner.
        final int matchMaxTurn = commander ? 25 : 20;

        for (int g = 0; g < games; g++) {
            Game game = newGame(commander);
            // The AI's minimax (SimulatedPlayer2) copies each player's MatchPlayer, so
            // every player must belong to a Match — match.addPlayer sets it. Without
            // this, calculateActions NPEs on a null MatchPlayer source at T1.M1.
            Match match = new FreeForAllMatch(new MatchOptions("make-magic batch",
                    commander ? "Commander Duel" : "Two Player Duel", true));
            Player playerA = addPlayer(game, match, "PlayerA", deckAPath, skill, range);
            Player playerB = addPlayer(game, match, "PlayerB", deckBPath, skill, range);

            GameOptions options = new GameOptions();
            options.testMode = false; // CRITICAL: real 7-card opening hands + the mulligan phase.
            setTurnCap(options, matchMaxTurn);
            game.setGameOptions(options);

            UUID starter = (g % 2 == 0) ? playerA.getId() : playerB.getId();
            GameDeadline deadline = GameDeadline.arm(game, MAX_GAME_WALLCLOCK_SECS);
            long t0 = System.currentTimeMillis();
            try {
                game.start(starter); // runs the entire game on this (main) thread; blocks until it ends.
            } finally {
                deadline.cancel();
            }
            long ms = System.currentTimeMillis() - t0;

            // A wall-clock timeout is a NON-DECISIVE, engine-defective game (see MAX_GAME_WALLCLOCK_SECS):
            // excluded from W/L/D, never a fabricated win for either seat.
            String winner = deadline.timedOut() ? "TIMEOUT/NON-DECISIVE"
                    : playerA.hasWon() ? "PlayerA" : playerB.hasWon() ? "PlayerB" : "DRAW/UNFINISHED";
            if (deadline.timedOut()) {
                System.out.println("XMAGEBATCH TIMEOUT reason=wallclock budgetSecs=" + MAX_GAME_WALLCLOCK_SECS
                        + " activePlayer=" + deadline.loopingPlayerId()
                        + " endTurn=" + game.getTurnNum());
            }
            System.out.println("XMAGEBATCH RESULT game=" + (g + 1) + "/" + games
                    + " winner=" + winner
                    + " endTurn=" + game.getTurnNum()
                    + " ended=" + game.hasEnded()
                    + " lifeA=" + playerA.getLife()
                    + " lifeB=" + playerB.getLife()
                    + " ms=" + ms
                    + " :: " + game.getWinner());
            // Forge-format terminator (parsed by extract_game_features / split_games) —
            // emitted here where hasWon() is reliably set. Candidate PlayerA = Ai(1).
            if (playerA.hasWon()) {
                System.out.printf("%nGame Result: Game %d ended in %d ms. Ai(1)-PlayerA has won!%n%n", g + 1, ms);
            } else if (playerB.hasWon()) {
                System.out.printf("%nGame Result: Game %d ended in %d ms. Ai(2)-PlayerB has won!%n%n", g + 1, ms);
            } else {
                System.out.printf("%nGame Result: Game %d ended in a Draw! Took %d ms.%n", g + 1, ms);
            }
        }
        System.out.flush();
        System.exit(0);
    }

    /**
     * SOLO (goldfish) harness: PlayerA (its real deck, optionally driven via the
     * {@code -Dmakemagic.driver} register-by-playerId seam) vs a do-nothing passer,
     * PlayerA ALWAYS on the play. Measures the OWN-turn kill and emits a
     * {@code GOLDFISH SUMMARY (OWN TURNS) ...} line (ported from GoldfishBatchTest).
     *
     * <p>When {@code commander} is true the goldfish is COMMANDER-NATIVE: the game is a
     * {@link CommanderDuel} (40 life + command zone, via {@link #newGame}, the SAME
     * construction the MATCH path uses), and the passer is a minimal LEGAL commander
     * shell (99 basics + a vanilla mono-color legendary commander on an {@code SB:} line
     * -> command zone, since CommanderDuel hardcodes {@code minimumDeckSize=100}), still
     * piloted by the do-nothing {@link PassingPlayer}. PlayerA's real commander rides in
     * via its own {@code SB:} line (the verified command-zone loader in {@link #addPlayer}).
     * When false the goldfish is the constructed 60-basics passer (byte-identical prior
     * behavior). Registration ({@link #registerDriver}) is unchanged and shared via
     * {@link #addPlayer}, so PlayerA still registers (or fails loud) in BOTH formats.
     */
    private static void runSolo(String deckAPath, int games, int skill, boolean commander)
            throws Exception {
        // Commander seats each player at RangeOfInfluence.ALL (multiplayer influence);
        // constructed keeps the ONE range that TwoPlayerDuel uses.
        RangeOfInfluence range = commander ? RangeOfInfluence.ALL : RangeOfInfluence.ONE;
        MakeMagicHooks.install();
        System.out.println("XMAGEBATCH scanning card database (first run builds it)...");
        CardScanner.scan();
        String driverFqcn = System.getProperty(DRIVER_PROP, "");
        String variant = driverFqcn.isEmpty() ? "cp7" : driverFqcn;
        System.out.println("XMAGEBATCH SOLO card db ready; deckA=" + deckAPath
                + " games=" + games + " skill=" + skill + " commander=" + commander
                + " driver=" + variant);

        // Passer: 60 Forest (constructed) or a legal 100-card commander shell.
        Path passerDeck = commander ? writeCommanderPasserDeck() : writePasserDeck();

        // MAX_TURN is the OWN-turn brick cap: a game that never kills sorts slowest at
        // MAX_TURN+1 (the goldfish never terminates by design once PlayerA decks itself
        // and LOSES, so game.start() always returns — this cap only shapes the sentinel).
        // Commander gets a longer cap (25 vs 20): a 40-life kill under command-tax ramp
        // is slower on average, so 25 own-turns keeps a legitimately slow-but-real kill
        // from being mis-sorted as a brick while still bounding a non-terminating game.
        final int maxTurn = commander ? 25 : 20;
        List<Integer> killTurns = new ArrayList<>(); // capped OWN kill turns for ALL games (bricks = maxTurn+1)
        List<Integer> realKills = new ArrayList<>(); // OWN kill turns for games that actually killed
        int bricks = 0;
        try {
            for (int g = 0; g < games; g++) {
                Game game = newGame(commander);
                Match match = new FreeForAllMatch(new MatchOptions("make-magic goldfish",
                        commander ? "Commander Duel" : "Two Player Duel", true));
                Player playerA = addPlayer(game, match, "PlayerA", deckAPath, skill, range);
                Player playerB = addPassingPlayer(game, match, "PlayerB", passerDeck.toString(), range);

                GameOptions options = new GameOptions();
                options.testMode = false; // real 7-card opening hands + the mulligan phase.
                // Per-game turn cap: PlayerA is ALWAYS on the play, so its OWN turns are the
                // odd global turns and its maxTurn-th own turn is global turn (2*maxTurn - 1).
                // Cap at global 2*maxTurn so that turn plays out fully, then the game STOPS at
                // the next untap instead of grinding to deckout. A non-closing game now ends
                // undecided at the cap -> killed=false -> the existing brick sentinel
                // (maxTurn+1), which is exactly what the gate's brick-cap guard already assumes.
                setTurnCap(options, maxTurn);
                game.setGameOptions(options);

                // Same HARD per-game deadline (see MAX_GAME_WALLCLOCK_SECS): a livelocking goldfish
                // is forced to return instead of hanging. A timeout leaves killed=false ⇒ the
                // existing brick sentinel (maxTurn+1) — a non-decisive game the gate already excludes.
                GameDeadline deadline = GameDeadline.arm(game, MAX_GAME_WALLCLOCK_SECS);
                long t0 = System.currentTimeMillis();
                try {
                    game.start(playerA.getId()); // PlayerA ALWAYS on the play (solo goldfish convention).
                } finally {
                    deadline.cancel();
                }
                long ms = System.currentTimeMillis() - t0;

                boolean killed = !deadline.timedOut() && (playerB.getLife() <= 0 || playerB.hasLost());
                int globalTurn = game.getTurnNum();
                // getTurnNum() is the GLOBAL turn counter (both players' turns). PlayerA is
                // ALWAYS on the play, so its OWN turns are the ODD global turns (1,3,5,...):
                //   ownTurn = (globalTurn + 1) / 2   (global 7 -> own 4, global 9 -> own 5).
                int ownEndTurn = (globalTurn + 1) / 2;
                int killTurn;
                if (killed) {
                    // Clamp a real kill to maxTurn so it can never sort at/after the brick
                    // sentinel (maxTurn+1). Without this, a genuine kill on own-turn >maxTurn
                    // would mis-sort as "slower than a brick" and corrupt medianAllOwn/distOwn.
                    killTurn = Math.min(ownEndTurn, maxTurn);
                    realKills.add(killTurn);
                } else {
                    killTurn = maxTurn + 1; // brick sentinel: the only value >maxTurn, so bricks always sort last.
                    bricks++;
                }
                killTurns.add(killTurn);
                System.out.println("GOLDFISH GAME " + (g + 1) + "/" + games + " deck=" + deckAPath
                        + " variant=" + variant + " killed=" + killed
                        + " ownKillTurn=" + (killed ? killTurn : "NONE")
                        + " globalTurn=" + globalTurn + " lifeB=" + playerB.getLife() + " ms=" + ms);
            }

            Collections.sort(killTurns);
            Collections.sort(realKills);
            double medianAll = median(killTurns);
            double medianKills = realKills.isEmpty() ? -1 : median(realKills);
            double meanKills = realKills.isEmpty() ? -1 : mean(realKills);
            int best = realKills.isEmpty() ? -1 : realKills.get(0);
            System.out.println("GOLDFISH SUMMARY (OWN TURNS) deck=" + deckAPath + " variant=" + variant
                    + " games=" + games + " maxTurn=" + maxTurn + " skill=" + skill
                    + " kills=" + realKills.size() + " bricks=" + bricks
                    + " medianAllOwn=" + medianAll + " medianKillsOwn=" + medianKills
                    + " meanKillsOwn=" + fmt(meanKills) + " bestOwn=" + best
                    + " distOwn=" + killTurns);
        } finally {
            try {
                Files.deleteIfExists(passerDeck);
            } catch (IOException ignored) {
                // temp file cleanup is best-effort.
            }
        }
        System.out.flush();
        System.exit(0);
    }

    // ==================== PERSISTENT-WORKER (Phase 2) ==================== //

    /** JSON DTO for one queued task line: {@code {id,fmt,a:{deck,driver:{cp,fqcn}?},b:{...}}}. */
    static final class Task {
        String id;
        String fmt; // "commander" | "constructed"
        Seat a;
        Seat b;
    }

    /** One seat: a staged deck path + an optional nested driver ({@code null} ⇒ pure CP7). */
    static final class Seat {
        String deck;
        DriverSpec driver; // nullable
    }

    /** A compiled driver: the classpath dir to load + the FQCN exposing {@code register(UUID)}. */
    static final class DriverSpec {
        String cp;
        String fqcn;
    }

    /** JSON DTO for one {@code RESULT {json}} line (Gson serializes the field names verbatim). */
    static final class Result {
        String id;
        String winner; // "A" | "B" | "DRAW"
        Integer kill_turn; // null when undecided
        long ms;
        List<String> markers;
        String log;
        String reason; // null for a normal decisive/undecided game; "timeout" for a forced deadline end.
    }

    /**
     * The persistent-worker loop. Scans the card DB ONCE at boot, then repeatedly prints
     * {@code READY}, reads one {@code TASK {json}} line from stdin, runs a single game, and
     * prints {@code RESULT {json}} (or {@code ERROR {json}} on a thrown game). Exits clean on
     * stdin EOF or after {@code maxGames} games (the governor respawns for the next batch).
     *
     * <p>The worker→governor protocol lines (READY / RESULT / ERROR) go on the REAL stdout —
     * the pipe the governor reads. The per-game TRANSCRIPT (every {@code System.out}/{@code
     * System.err} line the harness + dist AI emit during a game) is redirected into a per-game
     * file {@code <logdir>/<task_id>.log}; the two streams share ONE autoflushing PrintStream so
     * stdout+stderr interleave (the P4 parser needs the {@code Turn:}/HANDLOG context around the
     * driver-seam stderr markers) and the file grows live as the game plays.</p>
     */
    private static void runWorker(int maxGames) throws Exception {
        MakeMagicHooks.install(); // ONE long-lived collector; it resets its per-game state onGameStart.
        // The protocol pipe to the governor — captured before any per-game redirection.
        final PrintStream realOut = System.out;
        final PrintStream realErr = System.err;

        realErr.println("XMAGEBATCH WORKER scanning card database (first run builds it)...");
        realErr.flush();
        CardScanner.scan(); // ONCE, before the loop (concurrency-safe read after a --warm cold build).

        // Per-game transcripts land here. Overridable so the governor can point it at a staging
        // dir; defaults to <cwd>/logs (cwd already holds the H2 db/, per the run convention).
        String logDirProp = System.getProperty("makemagic.worker.logdir",
                System.getenv().getOrDefault("MAKE_MAGIC_WORKER_LOGDIR", "logs"));
        Path logsDir = Paths.get(logDirProp).toAbsolutePath();
        Files.createDirectories(logsDir);

        Gson gson = new Gson();
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));

        int played = 0;
        while (played < maxGames) {
            realOut.println("READY");
            realOut.flush();

            String line = in.readLine();
            if (line == null) {
                break; // stdin EOF → the governor closed the pipe; exit clean.
            }
            line = line.trim();
            if (line.isEmpty()) {
                continue;
            }
            if (!line.startsWith("TASK ")) {
                realErr.println("XMAGEBATCH WORKER: expected 'TASK {json}', got: " + line);
                realErr.flush();
                continue;
            }

            Task task;
            try {
                task = gson.fromJson(line.substring("TASK ".length()), Task.class);
            } catch (RuntimeException ex) {
                realErr.println("XMAGEBATCH WORKER: malformed TASK json: " + ex);
                realErr.flush();
                continue;
            }

            played++;
            try {
                Result result = runWorkerGame(task, logsDir, realOut, realErr);
                realOut.println("RESULT " + gson.toJson(result));
                realOut.flush();
            } catch (Exception ex) {
                String tid = task != null && task.id != null ? task.id : "?";
                Result err = new Result();
                // Reuse the RESULT DTO fields for a compact ERROR line: {id, exc}.
                java.util.Map<String, String> errBody = new java.util.HashMap<>();
                errBody.put("id", tid);
                errBody.put("exc", String.valueOf(ex));
                realOut.println("ERROR " + gson.toJson(errBody));
                realOut.flush();
                realErr.println("XMAGEBATCH WORKER: game " + tid + " threw: " + ex);
                ex.printStackTrace(realErr);
                realErr.flush();
            }
        }

        realErr.println("XMAGEBATCH WORKER exiting after " + played + " games (maxGames=" + maxGames + ").");
        realErr.flush();
        System.exit(0);
    }

    /**
     * Run ONE game for the worker: seat both players (each seat's driver, if any, loaded from a
     * fresh {@link URLClassLoader} and registered by playerId), redirect stdout+stderr into the
     * per-game transcript file, play to a decisive result or the turn cap, then RESTORE the
     * streams, DISCARD both classloaders, and RESET the seam registries for both seats (so a
     * driverless game that follows a driven one shows ZERO {@code DRIVER_*} markers). Returns the
     * {@link Result} to send on the protocol pipe.
     */
    private static Result runWorkerGame(Task task, Path logsDir, PrintStream realOut, PrintStream realErr)
            throws Exception {
        boolean commander = task.fmt != null && "commander".equalsIgnoreCase(task.fmt);
        RangeOfInfluence range = commander ? RangeOfInfluence.ALL : RangeOfInfluence.ONE;
        final int matchMaxTurn = commander ? 25 : 20;
        final int skill = 6;

        // Sanitize the task_id into a filesystem-safe basename before deriving the transcript
        // filename: the id embeds the deck path and the '|' cell separators, so a raw resolve()
        // would create nested/invalid path segments and throw FileNotFoundException. Replace
        // [/|:\] with '_' (BUG-2).
        Path logFile = logsDir.resolve(safeTaskName(task.id) + ".log");
        List<URLClassLoader> loaders = new ArrayList<>();
        UUID idA = null;
        UUID idB = null;

        // LAYER 2 — load-time forbidden-API scan (defense in depth; the Python gate is layer 1).
        // A driver may only enqueue LEGAL game actions; every terminal state must come from the
        // rules engine. BEFORE seating (no game runs, no classloader opens), scan each seat's
        // driver classpath for terminal-state references (Player.lost/won/... , Game.setWinner/
        // end). A hit REFUSES the driver and returns a TERMINAL non-decisive RESULT
        // (winner=none, reason=driver-rejected) — deduped + excluded from W/L/D, never requeued,
        // so a deterministically-bad driver cannot crash-loop the worker (contrast an ERROR,
        // which the governor's retry-counter may re-offer). Mirrors the existing timeout reason.
        List<String> lintHits = new ArrayList<>();
        for (Seat seat : new Seat[] {task.a, task.b}) {
            if (seat != null && seat.driver != null && seat.driver.cp != null) {
                lintHits.addAll(DriverClassLint.scanDir(Paths.get(seat.driver.cp)));
            }
        }
        if (!lintHits.isEmpty()) {
            String detail = String.join("; ", lintHits);
            realErr.println("XMAGEBATCH WORKER: driver-rejected (forbidden terminal-state API) "
                    + task.id + " :: " + detail);
            realErr.flush();
            Result rejected = new Result();
            rejected.id = task.id;
            rejected.winner = "none";
            rejected.kill_turn = null;
            rejected.ms = 0;
            rejected.log = null;
            rejected.reason = "driver-rejected";
            List<String> rmarkers = new ArrayList<>();
            rmarkers.add("reason=driver-rejected");
            rmarkers.add("decisive=false");
            rmarkers.add("forbidden=" + detail);
            rejected.markers = rmarkers;
            return rejected;
        }

        String winner;
        Integer killTurn;
        long ms;
        boolean timedOut = false;
        // The TERMINAL CAUSE of this game, derived from the engine end-state AFTER game.start()
        // returns (see deriveEndCause). Emitted as an ``end_cause=`` marker so the Python
        // classify_validity gate buckets on the cause, not on wall-clock. Default until derived.
        String endCause = "unknown";

        // Autoflushing, UTF-8 PrintStream: every println flushes, so the transcript grows live
        // and a tailer sees turn-by-turn progress. stdout+stderr share it → one interleaved file.
        // T3: a fresh per-game transcript byte cap wraps the FileOutputStream so a pathological
        // game cannot write an unbounded transcript (disk backstop). capForBudget returns the raw
        // FileOutputStream unwrapped when the cap is <= 0 (byte-identical prior behavior). The
        // wrapper is created per game, so the byte counter resets per game; closing the
        // PrintStream closes the cap which closes the FileOutputStream, so teardown is unchanged.
        PrintStream fileStream = new PrintStream(
                TranscriptCappingOutputStream.capForBudget(
                        new FileOutputStream(logFile.toFile(), false), MAX_TRANSCRIPT_BYTES),
                true, StandardCharsets.UTF_8);
        try {
            System.setOut(fileStream);
            System.setErr(fileStream);

            Game game = newGame(commander);
            Match match = new FreeForAllMatch(new MatchOptions("make-magic worker",
                    commander ? "Commander Duel" : "Two Player Duel", true));
            Player playerA = seatWorkerPlayer(game, match, "PlayerA", task.a, skill, range, loaders);
            // Capture idA IMMEDIATELY — before seating B. seatWorkerPlayer(A) may already have
            // registered A's quad and opened A's URLClassLoader, so if seating B then throws (a
            // bad opponent deck), the finally-block registriesReset(idA) must still cover A's
            // seam entries. Capturing idA only after BOTH seats leaks A's registry entries +
            // classloader on a driven-A / failing-B task (error-path leak, MAJOR).
            idA = playerA.getId();
            Player playerB = seatWorkerPlayer(game, match, "PlayerB", task.b, skill, range, loaders);
            idB = playerB.getId();

            GameOptions options = new GameOptions();
            options.testMode = false; // real 7-card opening hands + the mulligan phase.
            setTurnCap(options, matchMaxTurn);
            game.setGameOptions(options);

            // Arm the HARD per-game wall-clock deadline around this game.start() (see
            // MAX_GAME_WALLCLOCK_SECS). cancel() in finally clears any interrupt it set so the
            // deadline never bleeds into the NEXT game in this same worker.
            GameDeadline deadline = GameDeadline.arm(game, MAX_GAME_WALLCLOCK_SECS);
            long t0 = System.currentTimeMillis();
            try {
                game.start(playerA.getId()); // PlayerA (the subject) always on the play — deterministic.
            } finally {
                deadline.cancel();
            }
            ms = System.currentTimeMillis() - t0;
            timedOut = deadline.timedOut();
            UUID loopingId = timedOut ? deadline.loopingPlayerId() : null;

            if (timedOut) {
                // A wall-clock timeout is an ENGINE-DEFECTIVE, NON-DECISIVE game (the base CP7 AI
                // livelocks on an optional mana ability — a loop the real Comprehensive Rules
                // forbid, CR 104.4b / 720). It is NEITHER a draw NOR a loss for the looping seat;
                // it is EXCLUDED from W/L/D stats. Emit a TERMINAL result (dedup / cell-done / no
                // requeue) marked non-decisive: winner="none" (⇒ excluded from the Wilson W/L
                // denominator) + reason=timeout so the Python side can filter it explicitly.
                System.out.println("XMAGEBATCH TIMEOUT reason=wallclock budgetSecs=" + MAX_GAME_WALLCLOCK_SECS
                        + " activePlayer=" + loopingId
                        + " endTurn=" + game.getTurnNum());
            }

            // P4-contract terminators INTO the transcript (where hasWon() is reliably set).
            String batchWinner = timedOut ? "TIMEOUT/NON-DECISIVE"
                    : playerA.hasWon() ? "PlayerA"
                    : playerB.hasWon() ? "PlayerB" : "DRAW/UNFINISHED";
            System.out.println("XMAGEBATCH RESULT game=1/1 winner=" + batchWinner
                    + " endTurn=" + game.getTurnNum()
                    + " ended=" + game.hasEnded()
                    + " lifeA=" + playerA.getLife()
                    + " lifeB=" + playerB.getLife()
                    + " ms=" + ms
                    + " :: " + game.getWinner());
            if (!timedOut && playerA.hasWon()) {
                System.out.printf("%nGame Result: Game 1 ended in %d ms. Ai(1)-PlayerA has won!%n%n", ms);
            } else if (!timedOut && playerB.hasWon()) {
                System.out.printf("%nGame Result: Game 1 ended in %d ms. Ai(2)-PlayerB has won!%n%n", ms);
            } else if (timedOut) {
                System.out.printf("%nGame Result: Game 1 ended NON-DECISIVE (wall-clock timeout) after %d ms.%n%n", ms);
            } else {
                System.out.printf("%nGame Result: Game 1 ended in a Draw! Took %d ms.%n", ms);
            }

            // "none" ⇒ non-decisive; the Python _winner_bucket credits neither seat (excluded
            // from the W/L denominator, exactly like a draw), so a livelock never fabricates a win.
            winner = timedOut ? "none"
                    : playerA.hasWon() ? "A" : playerB.hasWon() ? "B" : "DRAW";
            killTurn = (!timedOut && (playerA.hasWon() || playerB.hasWon())) ? game.getTurnNum() : null;
            // Derive the terminal cause from the end-state while game/players are still in scope.
            endCause = deriveEndCause(game, playerA, playerB, timedOut, commander);
        } finally {
            // ALWAYS restore the protocol streams first, then flush+close the transcript.
            System.setOut(realOut);
            System.setErr(realErr);
            fileStream.flush();
            fileStream.close();
            // Reset the seam registries for BOTH seats (they clear() by playerId) and discard
            // each per-game classloader — the state-bleed + metaspace guard.
            registriesReset(idA);
            registriesReset(idB);
            for (URLClassLoader cl : loaders) {
                try {
                    cl.close();
                } catch (IOException ignored) {
                    // best-effort; a leaked handle is preferable to aborting the worker.
                }
            }
            loaders.clear();
        }

        Result result = new Result();
        result.id = task.id;
        result.winner = winner;
        result.kill_turn = killTurn;
        result.ms = ms;
        result.log = logFile.toAbsolutePath().toString();
        List<String> markers = new ArrayList<>();
        markers.add("seatA=" + (task.a != null && task.a.driver != null ? "driven" : "cp7"));
        markers.add("seatB=" + (task.b != null && task.b.driver != null ? "driven" : "cp7"));
        // The terminal-cause marker (same marker-list pattern as reason=/decisive=). The Python
        // classify_validity gate buckets on this: a legal decided/drawn cause is DECISIVE (even a
        // 22ms lethal kill), timeout is non-decisive, and an unknown cause on a decisive claim is
        // flagged INVALID. Emitted for EVERY game (endCause="timeout" on the deadline path).
        markers.add("end_cause=" + endCause);
        if (timedOut) {
            // Non-decisive: reason threads onto the wire so the governor / done-set can EXCLUDE
            // this game from W/L/D aggregation (the marker is a redundant, human-readable copy).
            result.reason = "timeout";
            markers.add("reason=timeout");
            markers.add("decisive=false");
        }
        result.markers = markers;
        return result;
    }

    /**
     * Derive the TERMINAL CAUSE of a finished game from the engine end-state (the Player boolean
     * flags + life + poison + empty-library + the CommanderInfoWatcher), for the {@code end_cause}
     * marker. XMage 1.4.60 retains NO loss-reason enum (the SBA {@code lostReason} is a discarded
     * local), so the honest, state-derivable taxonomy is:
     * <ul>
     *   <li>{@code timeout} — the wall-clock deadline fired (engine-defective livelock).</li>
     *   <li>{@code draw_game} — no winner (a real draw OR an unfinished turn-cap game).</li>
     *   <li>{@code concede} — the loser left the game (concede/leave sets {@code hasLeft()}).</li>
     *   <li>{@code draw_empty_library} — the loser drew from an empty library.</li>
     *   <li>{@code poison} — the loser holds >= 10 poison counters.</li>
     *   <li>{@code commander_damage} — some commander dealt the loser > 20 combat damage
     *       (checked BEFORE life, since a commander-damage loser is usually alive on life).</li>
     *   <li>{@code lethal_damage} — the loser is at <= 0 life (combat/burn/drain collapse here —
     *       the engine does not retain the discriminator).</li>
     *   <li>{@code state_loss} — any other rule/effect loss (unpaid Pact, "you lose" effects) —
     *       the discriminator is not retained, so these collapse honestly.</li>
     *   <li>{@code unknown} — ended but the loser matches none of the above (must be rare).</li>
     * </ul>
     */
    private static String deriveEndCause(Game game, Player playerA, Player playerB, boolean timedOut,
            boolean commander) {
        if (timedOut) {
            return "timeout";
        }
        boolean aWon = playerA.hasWon();
        boolean bWon = playerB.hasWon();
        if (!aWon && !bWon) {
            // No winner: a real draw or an unfinished turn-cap game — excluded from W/L either way.
            return "draw_game";
        }
        Player loser = aWon ? playerB : playerA;
        return causeOfLoss(game, loser, commander);
    }

    /** Classify WHY {@code loser} lost, from its retained end-state. Best-effort: any lookup that
     *  throws collapses to {@code state_loss} so cause derivation NEVER fails a real result. */
    private static String causeOfLoss(Game game, Player loser, boolean commander) {
        try {
            if (loser.hasLeft()) {
                return "concede";
            }
            if (loser.getLibrary().isEmptyDraw()) {
                return "draw_empty_library";
            }
            if (loser.getCountersCount(CounterType.POISON) >= 10) {
                return "poison";
            }
            if (commander && dealtLethalCommanderDamage(game, loser.getId())) {
                return "commander_damage";
            }
            if (loser.getLife() <= 0) {
                return "lethal_damage";
            }
            if (loser.hasLost()) {
                return "state_loss";
            }
        } catch (RuntimeException ex) {
            return "state_loss";
        }
        return "unknown";
    }

    /** True iff any player's commander dealt {@code loserId} > 20 combat damage (CR 903.14a),
     *  read from the per-commander {@link CommanderInfoWatcher} still on the game state. */
    private static boolean dealtLethalCommanderDamage(Game game, UUID loserId) {
        for (Player p : game.getPlayers().values()) {
            for (UUID cid : game.getCommandersIds(p, CommanderCardType.COMMANDER_OR_OATHBREAKER, false)) {
                CommanderInfoWatcher w = game.getState().getWatcher(CommanderInfoWatcher.class, cid);
                if (w == null) {
                    continue;
                }
                Map<UUID, Integer> dmg = w.getDamageToPlayer();
                Integer dealt = dmg == null ? null : dmg.get(loserId);
                if (dealt != null && dealt > 20) {
                    return true;
                }
            }
        }
        return false;
    }

    /**
     * Seat one worker player: a plain {@link ComputerPlayer7} on {@code seat.deck}, and — when
     * {@code seat.driver} is non-null — load that driver's FQCN from a FRESH
     * {@link URLClassLoader} over its classpath dir and reflectively invoke its
     * {@code public static void register(UUID)} on the player's final id (the frozen seam
     * convention). Unlike the -D {@link #registerDriver} path this seats BOTH seats and takes
     * the driver from the task, not a boot sysprop. The classloader is recorded in
     * {@code loaders} for post-game discard.
     */
    private static Player seatWorkerPlayer(Game game, Match match, String name, Seat seat, int skill,
            RangeOfInfluence range, List<URLClassLoader> loaders) throws Exception {
        if (seat == null || seat.deck == null) {
            throw new IllegalArgumentException(name + " seat has no deck");
        }
        DeckCardLists list = DeckImporter.importDeckFromFile(seat.deck, true);
        Deck deck = Deck.load(list, false, false);
        if (deck.getMaindeckCards().size() < 40) {
            throw new IllegalArgumentException(name + " deck too small (" + deck.getMaindeckCards().size()
                    + " cards) — did it fail to load? path=" + seat.deck);
        }
        ComputerPlayer7 player = new ComputerPlayer7(name, range, skill);
        if (MAX_THINK_TIME_SECS > 0) {
            player.setMaxThinkTimeSecs(MAX_THINK_TIME_SECS);
        }
        game.loadCards(deck.getCards(), player.getId());
        game.loadCards(deck.getSideboard(), player.getId());
        game.addPlayer(player, deck);
        match.addPlayer(player, deck);

        if (seat.driver != null) {
            if (seat.driver.cp == null || seat.driver.fqcn == null) {
                throw new IllegalArgumentException(name + " driver missing cp/fqcn");
            }
            URL[] urls = {Paths.get(seat.driver.cp).toUri().toURL()};
            URLClassLoader cl = new URLClassLoader(urls, XMageBatch.class.getClassLoader());
            loaders.add(cl); // recorded for post-game discard even if register() throws.
            Class<?> driverClass = cl.loadClass(seat.driver.fqcn);
            driverClass.getMethod("register", UUID.class).invoke(null, player.getId());
            System.out.println("DRIVER_REGISTERED fqcn=" + seat.driver.fqcn + " playerId=" + player.getId());
        }
        return player;
    }

    /**
     * Reset the four seam registries for {@code playerId} (each exposes a per-id
     * {@code clear(UUID)}). Called after every worker game for both seats so a subsequent
     * driverless game cannot observe a prior game's registered quad — the state-bleed guard.
     */
    private static void registriesReset(UUID playerId) {
        if (playerId == null) {
            return;
        }
        DriverBonus.clear(playerId);
        MacroRegistry.clear(playerId);
        SelectionRegistry.clear(playerId);
        MulliganRegistry.clear(playerId);
    }

    // ==================================================================== //

    private static Player addPlayer(Game game, Match match, String name, String deckPath, int skill,
            RangeOfInfluence range) throws Exception {
        DeckCardLists list = DeckImporter.importDeckFromFile(deckPath, true);
        Deck deck = Deck.load(list, false, false);
        // Floor of 40 covers both formats: constructed is >=40, commander's maindeck is
        // ~99 (the commander itself rides in as an SB: line -> command zone), so the
        // same guard catches a deck that failed to load in either mode.
        if (deck.getMaindeckCards().size() < 40) {
            throw new IllegalArgumentException(name + " deck too small (" + deck.getMaindeckCards().size()
                    + " cards) — did it fail to load? path=" + deckPath);
        }
        // PlayerA and PlayerB are BOTH plain ComputerPlayer7 — the in-search quad driver
        // does NOT replace the engine (that was the retired -Dmakemagic.driverA path); it
        // REGISTERS a quad by playerId below (see registerDriver), which the patched
        // minimax consults. Single construction point for every seat.
        ComputerPlayer7 player = new ComputerPlayer7(name, range, skill);
        // Bound each top-level AI decision's wall time (see MAX_THINK_TIME_SECS). CP7's
        // default is skill*3 (=18s at skill 6); we cap it lower so a single wide-board
        // decision can't burn the whole budget and a grinding wide-board turn can't blow
        // the Python stall watchdog. Pure time guard — best-move-so-far is returned on
        // timeout; rules/outcomes are unchanged. <=0 keeps XMage's default.
        if (MAX_THINK_TIME_SECS > 0) {
            player.setMaxThinkTimeSecs(MAX_THINK_TIME_SECS);
        }
        game.loadCards(deck.getCards(), player.getId());
        // The explicit sideboard load is REQUIRED in BOTH modes and is NOT redundant:
        // useDeck (via game.addPlayer -> player.useDeck) only puts the sideboard card
        // UUIDs into player.getSideboard(); it does NOT register the card objects in the
        // game. GameCommanderImpl.init walks player.getSideboard() and calls
        // game.getCard(cardId) to move the commander to the command zone — without this
        // loadCards that lookup returns null and the command zone comes up EMPTY (the
        // commander silently never appears). Verified: omitting it -> 0 commanders seated;
        // with it -> the commander lands in zone=COMMAND. There is no double-register
        // (loadCards only populates the game card map; init does the zone move once).
        game.loadCards(deck.getSideboard(), player.getId());
        game.addPlayer(player, deck);
        match.addPlayer(player, deck); // links player.getMatchPlayer() (needed by the AI sim).
        // Register the in-search quad driver — PlayerA ONLY, and only AFTER game/match
        // addPlayer so the playerId is final. This is the SINGLE registration point shared
        // by the match loop and the solo goldfish (they both seat PlayerA via addPlayer),
        // so the two paths can never diverge. PlayerB is never registered (stays pure CP7).
        if ("PlayerA".equals(name)) {
            registerDriver(player.getId());
        }
        return player;
    }

    /**
     * The in-search quad driver REGISTRATION seam (reflection-by-convention). When
     * {@code -Dmakemagic.driver=<FQCN>} is set, load that classpath-injected Driver class
     * and reflectively invoke its {@code public static void register(java.util.UUID)},
     * handing it PlayerA's final playerId — the Driver registers its quad
     * {@code (Φ, P, macro, S)} into the dist's {@code DriverBonus}/{@code MacroRegistry}/
     * {@code SelectionRegistry} keyed by that id, which the patched minimax consults. No
     * dist interface is added (the seam stays frozen); the contract is the {@code register}
     * method by convention.
     *
     * <p>Fail-loud: a missing class / missing {@code register(UUID)} / an exception thrown
     * inside registration PROPAGATES (this method throws), so a broken Driver aborts the run
     * rather than silently degrading to pure CP7 and masking a broken gate. Unset/empty ⇒
     * no registration ⇒ pure CP7 (unregistered seats no-op in every registry).</p>
     */
    private static void registerDriver(UUID playerId) throws Exception {
        String fqcn = System.getProperty(DRIVER_PROP, "");
        if (fqcn.isEmpty()) {
            return; // no driver → pure CP7 (the intelligence-preserving default).
        }
        Class<?> driverClass = Class.forName(fqcn);
        driverClass.getMethod("register", UUID.class).invoke(null, playerId);
        System.out.println("DRIVER_REGISTERED fqcn=" + fqcn + " playerId=" + playerId);
    }

    /**
     * Seat a do-nothing passer (goldfish opponent): a {@link PassingPlayer} that passes
     * every priority and never blocks, on a 60-basics deck. Mirrors GoldfishBatchTest's
     * plain (never-AI-simulated) PlayerB on {@code goldfish_lands.txt}.
     */
    private static Player addPassingPlayer(Game game, Match match, String name, String deckPath,
            RangeOfInfluence range) throws Exception {
        DeckCardLists list = DeckImporter.importDeckFromFile(deckPath, true);
        Deck deck = Deck.load(list, false, false);
        if (deck.getMaindeckCards().size() < 40) {
            throw new IllegalArgumentException(name + " passer deck too small ("
                    + deck.getMaindeckCards().size() + " cards) — did the basics fail to load? path=" + deckPath);
        }
        Player player = new PassingPlayer(name, range);
        game.loadCards(deck.getCards(), player.getId());
        game.loadCards(deck.getSideboard(), player.getId());
        game.addPlayer(player, deck);
        match.addPlayer(player, deck);
        return player;
    }

    /**
     * Build the game for a solo/match run: a {@link CommanderDuel} (40 life + command
     * zone, 7-card hand) in commander mode, else a {@link TwoPlayerDuel} (40 life, 7-card
     * hand). The SINGLE construction point shared by the MATCH loop and {@link #runSolo}
     * so the two paths can never diverge. CommanderDuel hardcodes minimumDeckSize=100
     * internally and takes the 5-arg ctor (attackOption, range, mulligan, startLife,
     * startHandSize); TwoPlayerDuel takes the 6-arg ctor (…, minimumDeckSize, …).
     */
    private static Game newGame(boolean commander) {
        return commander
                ? new CommanderDuel(MultiplayerAttackOption.LEFT, RangeOfInfluence.ALL,
                        MulliganType.GAME_DEFAULT.getMulligan(0), 40, 7)
                : new TwoPlayerDuel(MultiplayerAttackOption.LEFT, RangeOfInfluence.ONE,
                        MulliganType.GAME_DEFAULT.getMulligan(0), 40, 20, 7);
    }

    /**
     * Stamp a REAL per-game turn cap onto the options via XMage's built-in stop-on-turn
     * feature. {@code ownMaxTurn} is the OWN-turn budget (the solo goldfish sentinel: 25
     * commander / 20 constructed); the cap is the GLOBAL turn {@code 2*ownMaxTurn}. XMage's
     * {@code GameImpl.playTurn} calls {@code checkStopOnTurnOption()} at the TOP of each turn
     * and, when {@code stopOnTurn == turnNum} at the (default) {@code stopAtStep=UNTAP},
     * sets {@code winnerId=null} and returns false — breaking the play loop BEFORE that turn
     * is played. So turns {@code 1..2*ownMaxTurn-1} play out (PlayerA, always on the play in
     * solo, gets exactly {@code ownMaxTurn} own turns: the odd globals up to 2*ownMaxTurn-1)
     * and the game then STOPS undecided at the untap of global turn {@code 2*ownMaxTurn}.
     *
     * <p>This is ZERO gate-semantic change: the solo harness already clamps any counted kill
     * to {@code maxTurn} and the gate's brick-cap validity guard already REJECTS a driven
     * median at/beyond {@code maxTurn} as a clamp artifact (a brick masquerading as a pass).
     * A kill on own-turn {@code <= maxTurn} lands at global {@code <= 2*maxTurn-1} and still
     * happens; a would-be kill on own-turn {@code > maxTurn} (already gate-invalid) simply
     * becomes the brick sentinel instead of a clamped-to-maxTurn "kill". The cap only makes
     * a non-closing game STOP fast instead of deckout-grinding.</p>
     */
    private static void setTurnCap(GameOptions options, int ownMaxTurn) {
        // stopAtStep defaults to UNTAP in the GameOptions ctor; stopOnTurn is the GLOBAL turn
        // at whose untap the game halts (that turn is NOT played).
        options.stopOnTurn = 2 * ownMaxTurn;
    }

    /** Write the 60-basics goldfish opponent deck to a temp {@code N Cardname} .txt file. */
    private static Path writePasserDeck() throws IOException {
        Path f = Files.createTempFile("goldfish_lands", ".txt");
        Files.write(f, "60 Forest\n".getBytes(StandardCharsets.UTF_8));
        return f;
    }

    /**
     * Write the COMMANDER goldfish passer: a minimal LEGAL 100-card commander shell —
     * 99 Swamp maindeck + a vanilla mono-black legendary commander ({@code Yargle,
     * Glutton of Urborg}) on an {@code SB:} line so {@code GameCommanderImpl.init} moves
     * it to the command zone (CommanderDuel hardcodes minimumDeckSize=100). The passer
     * is piloted by the do-nothing {@link PassingPlayer}, so it never casts anything;
     * the commander only satisfies legality + gives the 40-life target a full deck.
     */
    private static Path writeCommanderPasserDeck() throws IOException {
        Path f = Files.createTempFile("goldfish_cmd_passer", ".txt");
        Files.write(f, "99 Swamp\nSB: 1 Yargle, Glutton of Urborg\n".getBytes(StandardCharsets.UTF_8));
        return f;
    }

    private static double median(List<Integer> xs) {
        int n = xs.size();
        if (n == 0) return -1;
        if (n % 2 == 1) return xs.get(n / 2);
        return (xs.get(n / 2 - 1) + xs.get(n / 2)) / 2.0;
    }

    private static double mean(List<Integer> xs) {
        double s = 0;
        for (int x : xs) s += x;
        return s / xs.size();
    }

    private static String fmt(double d) {
        return d < 0 ? "-1" : String.format("%.2f", d);
    }

    /**
     * Sanitize a task_id into a filesystem-safe transcript basename. The queue's task_id is
     * {@code subject|opponent|piloting|game_index} and each of subject/opponent can carry a
     * deck path, so the raw id may contain {@code / | : \} — none of which can appear in a
     * single path segment. Replace each with {@code _} so {@code <logdir>/<name>.log} is always
     * a valid, flat file (BUG-2). A null id maps to a stable placeholder.
     */
    private static String safeTaskName(String id) {
        return (id == null ? "task" : id).replaceAll("[/|:\\\\]", "_");
    }

    /**
     * A do-nothing goldfish opponent: passes every priority and never blocks. Extends
     * {@link ComputerPlayer} (not CP7 — no minimax needed; it takes no proactive action)
     * so the standalone game runs against a stationary 20-life target, the honest solo
     * clock. In the lab this fell out of the JUnit scaffold (PlayerB excluded from the
     * AI-simulated set); the standalone runner has no such scaffold, so the passivity is
     * expressed directly here.
     */
    static final class PassingPlayer extends ComputerPlayer {
        PassingPlayer(String name, RangeOfInfluence range) {
            super(name, range);
        }

        PassingPlayer(final PassingPlayer player) {
            super(player);
        }

        @Override
        public PassingPlayer copy() {
            return new PassingPlayer(this);
        }

        @Override
        public boolean priority(Game game) {
            pass(game); // never act: no lands, no spells, no activated abilities.
            return false;
        }

        @Override
        public void selectBlockers(Ability source, Game game, UUID defendingPlayerId) {
            // never block — leave the goldfish wide open so the OWN-turn clock is honest.
        }

        @Override
        public boolean chooseMulligan(Game game) {
            return false; // always keep — a 60-basics hand is never a mulligan and this can't stall.
        }
    }

    /**
     * A HARD per-game wall-clock deadline watcher (see {@link #MAX_GAME_WALLCLOCK_SECS}). One is
     * armed per game around a {@code game.start(...)} call; a daemon thread polls
     * {@code System.nanoTime()} and, once the budget elapses while the game is still running,
     * forces the game to return:
     *
     * <ol>
     *   <li>records the ACTIVE (looping) player id — the timeout LOSER;</li>
     *   <li>calls {@code game.end()} ({@code state.endGame()} ⇒ {@code checkIfGameIsOver()} true); and</li>
     *   <li>interrupts the game thread — XMage's inner play loops
     *       ({@code checkStateAndTriggered}, {@code play}, priority loops) all gate on
     *       {@code checkIfGameIsOver()}, which returns true when
     *       {@code Thread.currentThread().isInterrupted()} — the documented "third party tools /
     *       AI timeout" stop path — so even an intra-turn SBA livelock breaks out.</li>
     * </ol>
     *
     * <p>The watcher is a DAEMON (never blocks JVM exit) and is {@link #cancel() cancelled} in a
     * finally per game, which also CLEARS the game thread's interrupt flag via
     * {@code Thread.interrupted()} so a timeout in one game can never bleed into the next game run
     * in the same {@code --worker} process. {@code budgetSecs <= 0} ⇒ {@link #arm} is a no-op
     * (unbounded, prior behavior).</p>
     */
    static final class GameDeadline {
        private final Game game;
        private final Thread gameThread;
        private final long budgetNanos;
        private final Thread watcher;
        private volatile boolean timedOut = false;
        private volatile UUID loopingPlayerId = null;

        private GameDeadline(Game game, long budgetSecs) {
            this.game = game;
            this.gameThread = Thread.currentThread();
            this.budgetNanos = budgetSecs * 1_000_000_000L;
            this.watcher = new Thread(this::run, "makemagic-game-deadline");
            this.watcher.setDaemon(true);
        }

        /**
         * Arm a deadline around the CURRENT thread's imminent {@code game.start(...)}. Returns a
         * live watcher (already started) when {@code budgetSecs > 0}, else a disarmed no-op guard.
         * Call {@link #cancel()} in a finally.
         */
        static GameDeadline arm(Game game, long budgetSecs) {
            GameDeadline d = new GameDeadline(game, budgetSecs);
            if (budgetSecs > 0) {
                d.watcher.start();
            }
            return d;
        }

        private void run() {
            long t0 = System.nanoTime();
            try {
                while (System.nanoTime() - t0 <= budgetNanos) {
                    if (game.hasEnded()) {
                        return; // game finished normally within budget — nothing to force.
                    }
                    Thread.sleep(250);
                }
            } catch (InterruptedException ie) {
                return; // cancelled at game end — the normal per-game teardown.
            }
            if (game.hasEnded()) {
                return;
            }
            // Budget exceeded and the game is still running → force it to return.
            timedOut = true;
            try {
                loopingPlayerId = game.getActivePlayerId(); // the seat stuck in the loop = the loser.
            } catch (RuntimeException ignored) {
                // a racing read of active-player is best-effort; the interrupt below still stops it.
            }
            try {
                game.end(); // state.endGame() → checkIfGameIsOver() sees isGameOver().
            } catch (RuntimeException ignored) {
                // end() touches game state from off-thread; if it throws, the interrupt still stops it.
            }
            gameThread.interrupt(); // the sanctioned stop: checkIfGameIsOver() sees isInterrupted().
        }

        boolean timedOut() {
            return timedOut;
        }

        UUID loopingPlayerId() {
            return loopingPlayerId;
        }

        /**
         * Disarm the watcher after {@code game.start(...)} returns and CLEAR the game thread's
         * interrupt flag (set by a timeout) so the next game in the same worker starts clean.
         */
        void cancel() {
            watcher.interrupt();
            try {
                watcher.join(2000);
            } catch (InterruptedException ie) {
                // the game thread itself was interrupted by a timeout; cleared below.
            }
            // MUST run on the game thread: consumes any leftover interrupt so the NEXT game's
            // checkIfGameIsOver() does not immediately short-circuit to "over".
            Thread.interrupted();
        }
    }
}
