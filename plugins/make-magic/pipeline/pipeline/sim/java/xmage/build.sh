#!/usr/bin/env bash
#
# Reproducibly build the make-magic XMage sim harness jar + cache the reactor classpath.
#
# Produces (alongside this script):
#   - make-magic-xmage.jar   (org.makemagic.xmage.XMageBatch + mage.collectors.MakeMagicHooks)
# and, under $MAKE_MAGIC_XMAGE_HOME:
#   - make-magic-xmage-classpath.txt   (the reactor's transitive classpath, one line)
#     read at run time by pipeline/sim/xmage_runtime.py (no mvn at run time).
#
# Prereq: a BUILT XMage 1.4.60 reactor at $MAKE_MAGIC_XMAGE_HOME (a clone where
#   `mvn -pl Mage.Tests,Mage.Server.Plugins/Mage.Player.AI.MA -am install -DskipTests` has run so the module jars are in ~/.m2).
#
# Toolchain (no reliance on PATH):
#   JAVAC   javac to use (default: /opt/homebrew/opt/openjdk@17/bin/javac)
#   MVN     maven to use (default: mvn on PATH)
# Compiled with --release 17 (XMage 1.4.60 targets Java 8 bytecode / JDK 17 build).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

JAVAC="${JAVAC:-/opt/homebrew/opt/openjdk@17/bin/javac}"
JAR="${JAR:-/opt/homebrew/opt/openjdk@17/bin/jar}"
MVN="${MVN:-mvn}"

: "${MAKE_MAGIC_XMAGE_HOME:?set MAKE_MAGIC_XMAGE_HOME to a built XMage reactor (dir with Mage.Tests/)}"
XMAGE_HOME="$MAKE_MAGIC_XMAGE_HOME"
if [[ ! -d "$XMAGE_HOME/Mage.Tests" ]]; then
    echo "ERROR: $XMAGE_HOME has no Mage.Tests/ — not a built XMage reactor." >&2
    exit 1
fi

# Derive JAVA_HOME from javac (Homebrew launcher stubs need it) unless preset.
if [[ -z "${JAVA_HOME:-}" ]]; then
    real_javac="$(readlink -f "$JAVAC" 2>/dev/null || echo "$JAVAC")"
    export JAVA_HOME="$(cd "$(dirname "$real_javac")/.." && pwd)"
fi

CP_CACHE="$XMAGE_HOME/make-magic-xmage-classpath.txt"
echo ">> assembling reactor classpath -> $CP_CACHE"
# Include the "mad bot" (mage-player-ai-ma = ComputerPlayer7) explicitly — it is NOT a
# Mage.Tests dependency, so `-pl Mage.Tests -am` alone would omit it from the classpath
# and ComputerPlayer7 would fail to load at run time.
( cd "$XMAGE_HOME" && "$MVN" -q -pl Mage.Tests,Mage.Server.Plugins/Mage.Player.AI.MA -am \
    org.apache.maven.plugins:maven-dependency-plugin:3.6.1:build-classpath \
    -Dmdep.outputFile="$CP_CACHE" -Dmdep.includeScope=test )
[[ -s "$CP_CACHE" ]] || { echo "ERROR: classpath cache not written" >&2; exit 1; }
echo ">> classpath entries: $(tr ':' '\n' < "$CP_CACHE" | wc -l | tr -d ' ')"

echo ">> compiling XMageBatch + MakeMagicHooks"
rm -rf build && mkdir build
SOURCES=$(find src -name '*.java')
"$JAVAC" --release 17 -cp "$(cat "$CP_CACHE")" -d build $SOURCES

echo ">> packaging make-magic-xmage.jar"
"$JAR" --create --file make-magic-xmage.jar -C build .
echo ">> done:"
"$JAR" --list --file make-magic-xmage.jar | grep -E '\.class$'
