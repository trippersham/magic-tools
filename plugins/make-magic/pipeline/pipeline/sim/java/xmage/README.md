# make-magic XMage sim harness

A standalone headless **ComputerPlayer7-vs-ComputerPlayer7** XMage game runner that
emits the **same telemetry line contract as the Forge sim-AI harness**, so the
whole (M1/M2/M3-hardened) Forge parser in `pipeline/sim/telemetry.py` consumes
XMage logs verbatim — XMage is a drop-in with identical `PilotingProfile` +
`GameFeatures`, and the Python side needs no XMage-specific parser.

## Sources
- `src/org/makemagic/xmage/XMageBatch.java` — `main()`: `CardScanner.scan()`, build a
  `TwoPlayerDuel`, load two `ComputerPlayer7`s + decks, a fake `FreeForAllMatch`
  (so `player.getMatchPlayer()` is set — the AI's `SimulatedPlayer2` copies it),
  `gameOptions.testMode=false` (real 7-card hands), `game.start()`, then the
  Forge-format `Game Result:` terminator (after `start()`, where `hasWon()` is set).
- `src/mage/collectors/MakeMagicHooks.java` — a `mage.collectors`-package
  `DataCollector` shadow (that package has no public register API) that translates
  the real game into Forge lines: `HANDLOG turn=N event=turnstart|cast|gameend …`
  (piloting), `Turn:` / `Life:` / `Land:` (state-tracked), and `Damage:` parsed
  from XMage's own `"PlayerX loses N life at combat from <src>"` message — the
  authoritative combat/burn flag, not a guess. Slot map: PlayerA=`Ai(1)`
  (candidate), PlayerB=`Ai(2)`.

## Why it works
- `game.start()` runs the entire game on the calling thread; XMage's game-thread
  guard whitelists the thread named `main`, so `main()` satisfies it natively.
- The `DataCollector` hooks skip AI simulation sub-games (`game.isSimulation()`),
  so only the real game is logged.
- Damage **to a player** is `"loses N life at combat from X"` (via `informPlayers`
  → `onGameLog`), NOT `"deals N damage"` (that is permanent-to-permanent only).

## Build / run
Dev build compiles against the XMage reactor's classpath (assembled via Maven).
See the spike driver `~/mtg-sim-lab/xmage-lab/spike/build_and_run.sh` for the
reference invocation; the SHADED MIT production jar + the `XMageEngine` SimEngine
wrapper + the CI license audit are task 2.3.

    XMage jars: build the reactor (`mvn -pl Mage.Tests,Mage.Server.Plugins/Mage.Player.AI.MA -am install -DskipTests`) at
    a local XMage 1.4.60 clone; assemble the classpath with
    `mvn dependency:build-classpath`; compile the two sources; run with
    cwd=Mage.Tests (so the H2 card db/ resolves).

## Telemetry parity
Verified by `tests/test_telemetry_xmage.py` against the real fixture
`tests/fixtures/xmage/cp7_uw_mirror.handlog` (4-game UW mirror): the Forge parser
yields counter-fire > 0 (the CP7 signature Forge's counter-blind AI lacks) plus
full `GameFeatures` (kill_turn / wincon=combat / margin). Licensing: XMage is MIT,
this repo is GPL-3 — MIT-in-GPL is compatible.
