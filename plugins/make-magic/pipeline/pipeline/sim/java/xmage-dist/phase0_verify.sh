#!/usr/bin/env bash
#
# Phase-0 verification (shared per-deck XMage driver): prove the two ported seams in
# the productionized XMageBatch against the LOCALLY-built dist jar.
#
#   A) SOLO + DRIVER   -- a dummy driver injected by FQCN via -Dmakemagic.driverA runs a
#                         1-game goldfish to a `GOLDFISH SUMMARY (OWN TURNS) ...` line.
#   B) SOLO DRIVERLESS -- the same solo run with no -D falls back to CP7 (variant=cp7).
#   C) MATCH (regress) -- the pre-existing deckA-vs-deckB match still emits XMAGEBATCH
#                         RESULT + the Forge-format `Game Result:` terminator.
#
# It compiles a throwaway driver against the dist jar (also proving a driver needs
# NOTHING outside the shaded jar), then runs all three and asserts on stdout.
#
# Env:
#   JAVA   java 17 launcher   (default: openjdk@17 if present, else PATH `java`)
#   JAR    the dist jar        (default: target/make-magic-xmage-dist.jar next to build.sh)
#   DECK   a `N Cardname` .txt  (default: the lab MonoR_Aggro.txt if present)
# Run from a dir holding db/ (CardScanner builds it on first use), or let it cd to the
# XDG cache if that db/ exists.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

JAVA="${JAVA:-}"
if [[ -z "$JAVA" ]]; then
    if [[ -x /opt/homebrew/opt/openjdk@17/bin/java ]]; then JAVA=/opt/homebrew/opt/openjdk@17/bin/java; else JAVA=java; fi
fi
JAVAC="$(dirname "$JAVA")/javac"
JAR="${JAR:-$HERE/target/make-magic-xmage-dist.jar}"
DECK="${DECK:-$HOME/mtg-sim-lab/xmage-lab/mage/Mage.Tests/MonoR_Aggro.txt}"

[[ -f "$JAR" ]]  || { echo "FAIL: dist jar not found: $JAR (run ./build.sh first)"  >&2; exit 1; }
[[ -f "$DECK" ]] || { echo "FAIL: deck not found: $DECK (set DECK=<a N-Cardname .txt>)" >&2; exit 1; }

# A warm H2 db/ must be in cwd. Prefer the XDG cache if it already has one.
if [[ ! -d db && -d "$HOME/.local/share/make-magic/xmage/db" ]]; then
    cd "$HOME/.local/share/make-magic/xmage"
fi

echo ">> compiling throwaway driver against the dist jar (no external deps)"
DSRC="$(mktemp -d)"; DCLS="$(mktemp -d)"
trap 'rm -rf "$DSRC" "$DCLS"' EXIT
mkdir -p "$DSRC/makemagic/testdriver"
cat > "$DSRC/makemagic/testdriver/DummyDriver.java" <<'EOF'
package makemagic.testdriver;
import mage.constants.RangeOfInfluence;
import mage.player.ai.ComputerPlayer7;
public class DummyDriver extends ComputerPlayer7 {
    public DummyDriver(String name, RangeOfInfluence range, int skill) { super(name, range, skill); }
    public DummyDriver(final DummyDriver d) { super(d); }
}
EOF
"$JAVAC" -cp "$JAR" -d "$DCLS" "$DSRC/makemagic/testdriver/DummyDriver.java"
echo ">> driver compiled OK"

pass=0
assert() { # <label> <needle> <output>
    if grep -qE "$2" <<<"$3"; then echo ">> PASS: $1"; else
        echo ">> FAIL: $1 (missing /$2/)"; echo "$3" | tail -20; exit 1; fi
    pass=$((pass+1))
}

echo ">> A: SOLO + DRIVER"
OUT_A="$("$JAVA" -cp "$JAR:$DCLS" -Dmakemagic.driverA=makemagic.testdriver.DummyDriver \
        org.makemagic.xmage.XMageBatch "$DECK" --solo 1 2>&1)"
assert "A driver injected by FQCN"     "driverA=makemagic.testdriver.DummyDriver" "$OUT_A"
assert "A GOLDFISH SUMMARY w/ own-turn" "GOLDFISH SUMMARY \(OWN TURNS\).*medianKillsOwn=" "$OUT_A"

echo ">> B: SOLO DRIVERLESS"
OUT_B="$("$JAVA" -cp "$JAR" org.makemagic.xmage.XMageBatch "$DECK" --solo 1 2>&1)"
assert "B driverless -> cp7"           "variant=cp7"                                "$OUT_B"
assert "B GOLDFISH SUMMARY"            "GOLDFISH SUMMARY \(OWN TURNS\).*medianKillsOwn=" "$OUT_B"

echo ">> C: MATCH (regression)"
OUT_C="$("$JAVA" -cp "$JAR" org.makemagic.xmage.XMageBatch "$DECK" "$DECK" 1 6 2>&1)"
assert "C match RESULT line"           "XMAGEBATCH RESULT game=1/1"                 "$OUT_C"
assert "C Forge-format terminator"     "Game Result: Game 1 ended"                  "$OUT_C"

echo ">> ALL $pass ASSERTIONS PASSED"
