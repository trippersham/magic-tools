#!/usr/bin/env bash
#
# Phase-3 verification (in-search quad XMage driver): prove the register-by-playerId
# seam in the productionized XMageBatch against the LOCALLY-built dist jar.
#
#   A) SOLO + DRIVER   -- a dummy driver named by FQCN via -Dmakemagic.driver has its
#                         static register(UUID) reflectively invoked on PlayerA
#                         (DRIVER_REGISTERED + a driver-side DUMMY_DRIVER_REGISTER marker)
#                         and the 1-game goldfish still emits a `GOLDFISH SUMMARY` line.
#   B) SOLO DRIVERLESS -- the same solo run with no -D falls back to CP7 (variant=cp7)
#                         and emits NO DRIVER_REGISTERED line.
#   C) MATCH (regress) -- the pre-existing deckA-vs-deckB match still emits XMAGEBATCH
#                         RESULT + the Forge-format `Game Result:` terminator.
#   D) DRIVEN MATCH    -- AC2: the named driver REGISTERS on PlayerA (DRIVER_REGISTERED)
#                         AND the driven match runs to a decisive RESULT line.
#   E) BAD FQCN LOUD   -- an unresolvable -Dmakemagic.driver exits non-zero and emits
#                         NO summary (headline safety property: no silent CP7 fallback).
#
# It compiles a throwaway driver exposing `static register(UUID)` against the dist jar
# (also proving a driver needs NOTHING outside the shaded seam registries), then runs all
# cases and asserts on stdout/stderr + exit code.
#
# NOTE: until the dist jar is re-cut at Phase 6.6, the dist bundles the PRIOR shaded
# XMageBatch. The committed harness jar (make-magic-xmage.jar, freshly built by
# pipeline/sim/java/xmage/build.sh) is PREPENDED on the classpath so the current
# register-by-playerId XMageBatch shadows the dist's stale copy — the same shadow the
# Phase-3 behavioral run uses. After the P6.6 dist rebuild the shadow is a no-op.
#
# Env:
#   JAVA     java launcher     (default: openjdk@17 if present, else PATH `java`)
#   JAVAC    javac             (default: sibling of JAVA)
#   JAR      the dist jar       (default: target/make-magic-xmage-dist.jar next to build.sh)
#   HARNESS  committed harness  (default: ../xmage/make-magic-xmage.jar)
#   DECK     a `N Cardname` .txt (default: the lab MonoR_Aggro.txt if present)
# Run from a dir holding db/ (CardScanner builds it on first use), or let it cd to the
# XDG cache if that db/ exists.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

JAVA="${JAVA:-}"
if [[ -z "$JAVA" ]]; then
    if [[ -x /opt/homebrew/opt/openjdk@17/bin/java ]]; then JAVA=/opt/homebrew/opt/openjdk@17/bin/java; else JAVA=java; fi
fi
JAVAC="${JAVAC:-$(dirname "$JAVA")/javac}"
JAR="${JAR:-$HERE/target/make-magic-xmage-dist.jar}"
HARNESS="${HARNESS:-$HERE/../xmage/make-magic-xmage.jar}"
DECK="${DECK:-$HOME/mtg-sim-lab/xmage-lab/mage/Mage.Tests/MonoR_Aggro.txt}"

[[ -f "$JAR" ]]     || { echo "FAIL: dist jar not found: $JAR (run ./build.sh first)"  >&2; exit 1; }
[[ -f "$HARNESS" ]] || { echo "FAIL: harness jar not found: $HARNESS (run pipeline/sim/java/xmage/build.sh)" >&2; exit 1; }
[[ -f "$DECK" ]]    || { echo "FAIL: deck not found: $DECK (set DECK=<a N-Cardname .txt>)" >&2; exit 1; }

# The current harness XMageBatch must WIN class-loading over the dist's stale shaded copy.
CP="$HARNESS:$JAR"

# A warm H2 db/ must be in cwd. Prefer the XDG cache if it already has one.
if [[ ! -d db && -d "$HOME/.local/share/make-magic/xmage/db" ]]; then
    cd "$HOME/.local/share/make-magic/xmage"
fi

echo ">> compiling throwaway register-by-playerId driver against the dist jar (seam only)"
DSRC="$(mktemp -d)"; DCLS="$(mktemp -d)"
trap 'rm -rf "$DSRC" "$DCLS"' EXIT
mkdir -p "$DSRC/makemagic/testdriver"
cat > "$DSRC/makemagic/testdriver/DummyDriver.java" <<'EOF'
package makemagic.testdriver;
import java.util.UUID;
import mage.player.ai.score.DriverBonus;
public final class DummyDriver {
    private DummyDriver() {}
    // The quad-ingestion contract: XMageBatch reflectively invokes this on PlayerA's id.
    public static void register(UUID playerId) {
        // A driver-side marker proving the reflective invoke reached this class (a silent
        // CP7 fallback would never print it), then register a trivial no-op-ish Phi so the
        // seam registry is exercised for this seat.
        System.err.println("DUMMY_DRIVER_REGISTER playerId=" + playerId);
        DriverBonus.register(playerId, (game, pid) -> 0);
    }
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
refute() { # <label> <needle> <output>
    if grep -qE "$2" <<<"$3"; then
        echo ">> FAIL: $1 (unexpected /$2/)"; echo "$3" | tail -20; exit 1;
    else echo ">> PASS: $1"; pass=$((pass+1)); fi
}

echo ">> A: SOLO + DRIVER"
OUT_A="$("$JAVA" -cp "$CP:$DCLS" -Dmakemagic.driver=makemagic.testdriver.DummyDriver \
        org.makemagic.xmage.XMageBatch "$DECK" --solo 1 2>&1)"
assert "A DRIVER_REGISTERED for PlayerA"  "DRIVER_REGISTERED fqcn=makemagic.testdriver.DummyDriver" "$OUT_A"
assert "A driver-side register marker"    "DUMMY_DRIVER_REGISTER playerId="            "$OUT_A"
assert "A GOLDFISH SUMMARY w/ own-turn"   "GOLDFISH SUMMARY \(OWN TURNS\).*medianKillsOwn=" "$OUT_A"

echo ">> B: SOLO DRIVERLESS"
OUT_B="$("$JAVA" -cp "$CP" org.makemagic.xmage.XMageBatch "$DECK" --solo 1 2>&1)"
assert "B driverless -> cp7"              "variant=cp7"                                "$OUT_B"
refute "B no DRIVER_REGISTERED"           "DRIVER_REGISTERED"                          "$OUT_B"
assert "B GOLDFISH SUMMARY"               "GOLDFISH SUMMARY \(OWN TURNS\).*medianKillsOwn=" "$OUT_B"

echo ">> C: MATCH (regression)"
OUT_C="$("$JAVA" -cp "$CP" org.makemagic.xmage.XMageBatch "$DECK" "$DECK" 1 6 2>&1)"
assert "C match RESULT line"              "XMAGEBATCH RESULT game=1/1"                 "$OUT_C"
assert "C Forge-format terminator"        "Game Result: Game 1 ended"                  "$OUT_C"

# D: DRIVEN MATCH — proves AC2 (the driver registers IN A MATCH, not just solo). The
# named driver must register on PlayerA AND the driven match must run to a decisive line.
echo ">> D: DRIVEN MATCH"
OUT_D="$("$JAVA" -cp "$CP:$DCLS" -Dmakemagic.driver=makemagic.testdriver.DummyDriver \
        org.makemagic.xmage.XMageBatch "$DECK" "$DECK" 1 6 2>&1)"
assert "D DRIVER_REGISTERED for PlayerA"  "DRIVER_REGISTERED fqcn=makemagic.testdriver.DummyDriver" "$OUT_D"
assert "D driven match RESULT line"       "XMAGEBATCH RESULT game=1/1"                 "$OUT_D"

# E: BAD FQCN FAILS LOUD — the headline safety property: an unresolvable driver must
# exit non-zero and emit NO summary (never a silent CP7 fallback of a "driven" result).
echo ">> E: BAD FQCN FAILS LOUD"
set +e
OUT_E="$("$JAVA" -cp "$CP" -Dmakemagic.driver=does.not.Exist \
        org.makemagic.xmage.XMageBatch "$DECK" --solo 1 2>&1)"
RC_E=$?
set -e
if [[ "$RC_E" -ne 0 ]]; then echo ">> PASS: E bad FQCN exits non-zero (rc=$RC_E)"; pass=$((pass+1));
    else echo ">> FAIL: E bad FQCN exited 0 (expected non-zero)"; echo "$OUT_E" | tail -20; exit 1; fi
if grep -qE "GOLDFISH SUMMARY" <<<"$OUT_E"; then
    echo ">> FAIL: E bad FQCN emitted a GOLDFISH SUMMARY (silent fallback!)"; echo "$OUT_E" | tail -20; exit 1;
    else echo ">> PASS: E bad FQCN emits NO GOLDFISH SUMMARY"; pass=$((pass+1)); fi

echo ">> ALL $pass ASSERTIONS PASSED"
