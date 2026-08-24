#!/usr/bin/env bash
#
# Build the make-magic XMage sim distributable — ONE shaded jar (headless CP7 engine
# + the make-magic harness) that pipeline/sim/xmage_runtime.py fetches at runtime so
# users never build the reactor. See pom.xml for the license-clean dependency rationale.
#
# Produces (under target/):
#   make-magic-xmage-dist.jar   (the shaded, runnable fat jar; NOT committed — see .gitignore)
#
# Prereq: the org.mage:*:1.4.60 reactor artifacts in the local Maven repo (~/.m2),
#   i.e. a magefree/mage @ 1.4.60 clone where `mvn -pl Mage.Tests,Mage.Server.Plugins/Mage.Player.AI.MA,Mage.Server.Plugins/Mage.Game.CommanderDuel -am install -DskipTests`
#   has run. In CI (xmage-dist-release.yml) that clone+install is the preceding step.
#
# Toolchain (no reliance on PATH):
#   JAVA_HOME  a JDK 17 (default: derived from openjdk@17 if present)
#   MVN        maven to use (default: mvn on PATH)
#
# Flags:
#   --print-sha   after building, print the jar's SHA256 (copy into xmage_runtime's pin).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

MVN="${MVN:-mvn}"
if [[ -z "${JAVA_HOME:-}" ]]; then
    if [[ -x /opt/homebrew/opt/openjdk@17/bin/javac ]]; then
        real_javac="$(readlink -f /opt/homebrew/opt/openjdk@17/bin/javac 2>/dev/null || echo /opt/homebrew/opt/openjdk@17/bin/javac)"
        export JAVA_HOME="$(cd "$(dirname "$real_javac")/.." && pwd)"
    fi
fi
echo ">> JAVA_HOME=${JAVA_HOME:-<unset>}  MVN=$MVN"

echo ">> mvn package (shade)"
"$MVN" -q -DskipTests package

JAR="$HERE/target/make-magic-xmage-dist.jar"
[[ -f "$JAR" ]] || { echo "ERROR: shaded jar not produced at $JAR" >&2; exit 1; }
SIZE_MB=$(( $(stat -f '%z' "$JAR" 2>/dev/null || stat -c '%s' "$JAR") / 1048576 ))
echo ">> built: $JAR (${SIZE_MB} MB)"

if [[ "${1:-}" == "--print-sha" ]]; then
    if command -v sha256sum >/dev/null 2>&1; then SHA="$(sha256sum "$JAR" | awk '{print $1}')";
    else SHA="$(shasum -a 256 "$JAR" | awk '{print $1}')"; fi
    echo ">> SHA256=$SHA"
fi
