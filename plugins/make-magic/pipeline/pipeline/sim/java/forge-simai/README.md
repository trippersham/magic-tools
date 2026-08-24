# make-magic Forge sim-AI harness

A tiny Java harness that runs a headless Forge match with Forge's built-in
**simulation AI** (`AIOption.USE_SIMULATION`) enabled — the hook the stock `sim`
CLI verb does not expose — and streams `HANDLOG` decision-point instrumentation.

## Contents

- `src/org/makemagic/simai/SimAIMatch.java` — the harness (`Main-Class`).
  Enables `USE_SIMULATION` for both AI players and registers a real-time
  `HANDLOG` event-bus subscriber.
- `patch/forge/game/staticability/StaticAbilityContinuous.java` — a copy of
  Forge's class carrying its original FQN (`forge.game.staticability.…`) plus an
  NPE guard for a `MayPlayPlayer` crash the sim AI hits inside a copied game.
  When the built jar is placed **first** on the classpath, this class shadows
  Forge's crash-prone original.

Both files are Forge-derived and therefore GPL-3 (matching upstream Forge and
this repository); see the repo-root `LICENSE`.

## Build

```bash
export MAKE_MAGIC_FORGE_HOME=~/mtg-sim-lab/forge   # dir with forge-gui-desktop-*-jar-with-dependencies.jar
./build.sh                                          # or: ./build.sh --forge-jar /abs/forge.jar
```

Produces `dist/make-magic-forge-simai.jar` (single jar, both classes,
`Main-Class: org.makemagic.simai.SimAIMatch`). The jar is committed so the wheel
+ tests are deterministic without a JDK at install time; CI rebuilds to verify
reproducibility. `build/` (intermediate `.class`) is git-ignored.

## Run (smoke)

Classpath ordering is load-bearing — the harness jar MUST come first so its
`StaticAbilityContinuous` shadows Forge's:

```bash
cd "$MAKE_MAGIC_FORGE_HOME"   # so res/ resolves
<runjava> -Dapple.awt.UIElement=true -Xmx3g \
  -cp "dist/make-magic-forge-simai.jar:$FORGE_JAR" \
  org.makemagic.simai.SimAIMatch -d <deckA.dck> <deckB.dck> -n 1 -c 60 -sim 1
```

Never pass `-Djava.awt.headless=true` (Forge exits 1 silently); on macOS use
`-Dapple.awt.UIElement=true`.
