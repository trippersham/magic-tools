package org.makemagic.xmage;

import mage.cards.decks.Deck;
import mage.cards.decks.DeckCardLists;
import mage.cards.decks.importer.DeckImporter;
import mage.cards.repository.CardScanner;
import mage.collectors.MakeMagicHooks;
import mage.constants.MultiplayerAttackOption;
import mage.constants.RangeOfInfluence;
import mage.game.FreeForAllMatch;
import mage.game.Game;
import mage.game.GameOptions;
import mage.game.TwoPlayerDuel;
import mage.game.match.Match;
import mage.game.match.MatchOptions;
import mage.game.mulligan.MulliganType;
import mage.player.ai.ComputerPlayer7;
import mage.players.Player;

import java.util.UUID;

/**
 * Phase-2 SPIKE (task 2.1): a STANDALONE headless ComputerPlayer7-vs-ComputerPlayer7
 * XMage game, OUTSIDE the JUnit/surefire scaffold.
 *
 * The gate this proves: two raw production {@link ComputerPlayer7}s play a
 * {@link TwoPlayerDuel} to a decisive result with REAL 7-card opening hands +
 * mulligans (gameOptions.testMode=false — NOT the 0-card testMode path), driven
 * only by {@code game.start()} on the {@code main} thread (which XMage's
 * game-thread guard whitelists). Opening hands + the game log are surfaced by
 * {@link MakeMagicHooks}.
 *
 * Usage:
 *   java -cp &lt;xmage-classpath&gt;:&lt;this&gt; org.makemagic.xmage.XMageBatch \
 *       &lt;deckA&gt; &lt;deckB&gt; [games] [skill] ; run with cwd = Mage.Tests (so the H2 db/ resolves).
 */
public class XMageBatch {

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
        if (args.length < 2) {
            System.err.println("usage: XMageBatch <deckA> <deckB> [games] [skill]  |  XMageBatch --warm");
            System.exit(2);
        }
        String deckAPath = args[0];
        String deckBPath = args[1];
        int games = args.length > 2 ? Integer.parseInt(args[2]) : 1;
        int skill = args.length > 3 ? Integer.parseInt(args[3]) : 6;

        MakeMagicHooks.install();
        System.out.println("XMAGEBATCH scanning card database (first run builds it)...");
        CardScanner.scan();
        System.out.println("XMAGEBATCH card db ready; deckA=" + deckAPath + " deckB=" + deckBPath
                + " games=" + games + " skill=" + skill);

        for (int g = 0; g < games; g++) {
            Game game = new TwoPlayerDuel(MultiplayerAttackOption.LEFT, RangeOfInfluence.ONE,
                    MulliganType.GAME_DEFAULT.getMulligan(0), 40, 20, 7);
            // The AI's minimax (SimulatedPlayer2) copies each player's MatchPlayer, so
            // every player must belong to a Match — match.addPlayer sets it. Without
            // this, calculateActions NPEs on a null MatchPlayer source at T1.M1.
            Match match = new FreeForAllMatch(new MatchOptions("make-magic batch", "Two Player Duel", true));
            Player playerA = addPlayer(game, match, "PlayerA", deckAPath, skill);
            Player playerB = addPlayer(game, match, "PlayerB", deckBPath, skill);

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

    private static Player addPlayer(Game game, Match match, String name, String deckPath, int skill) throws Exception {
        DeckCardLists list = DeckImporter.importDeckFromFile(deckPath, true);
        Deck deck = Deck.load(list, false, false);
        if (deck.getMaindeckCards().size() < 40) {
            throw new IllegalArgumentException(name + " deck too small (" + deck.getMaindeckCards().size()
                    + " cards) — did it fail to load? path=" + deckPath);
        }
        Player player = new ComputerPlayer7(name, RangeOfInfluence.ONE, skill);
        game.loadCards(deck.getCards(), player.getId());
        game.loadCards(deck.getSideboard(), player.getId());
        game.addPlayer(player, deck);
        match.addPlayer(player, deck); // links player.getMatchPlayer() (needed by the AI sim).
        return player;
    }
}
