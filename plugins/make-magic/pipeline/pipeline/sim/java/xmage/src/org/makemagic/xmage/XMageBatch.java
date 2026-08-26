package org.makemagic.xmage;

import mage.abilities.Ability;
import mage.cards.decks.Deck;
import mage.cards.decks.DeckCardLists;
import mage.cards.decks.importer.DeckImporter;
import mage.cards.repository.CardScanner;
import mage.collectors.MakeMagicHooks;
import mage.constants.MultiplayerAttackOption;
import mage.constants.RangeOfInfluence;
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
import mage.players.Player;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
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
            game.setGameOptions(options);

            UUID starter = (g % 2 == 0) ? playerA.getId() : playerB.getId();
            long t0 = System.currentTimeMillis();
            game.start(starter); // runs the entire game on this (main) thread; blocks until it ends.
            long ms = System.currentTimeMillis() - t0;

            String winner = playerA.hasWon() ? "PlayerA" : playerB.hasWon() ? "PlayerB" : "DRAW/UNFINISHED";
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
                game.setGameOptions(options);

                long t0 = System.currentTimeMillis();
                game.start(playerA.getId()); // PlayerA ALWAYS on the play (solo goldfish convention).
                long ms = System.currentTimeMillis() - t0;

                boolean killed = playerB.getLife() <= 0 || playerB.hasLost();
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
        Player player = new ComputerPlayer7(name, range, skill);
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
}
