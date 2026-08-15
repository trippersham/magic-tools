#!/usr/bin/env bash
#
# Reproducibly build the make-magic Forge sim-AI harness jar.
#
# Produces dist/make-magic-forge-simai.jar containing BOTH:
#   - org.makemagic.simai.SimAIMatch            (the harness; Main-Class)
#   - forge.game.staticability.StaticAbilityContinuous  (the NPE-guard shadow)
#
# The shadow class deliberately reuses Forge's FQN so that, when this jar is
# placed FIRST on the classpath (ahead of the Forge jar), our patched class
# wins the class-load and shadows Forge's crash-prone original.
#
# Toolchain is passed in explicitly (no reliance on PATH javac):
#   JAVAC          javac to use (default: /opt/homebrew/opt/openjdk@17/bin/javac)
#   MAKE_MAGIC_FORGE_HOME  dir holding forge-gui-desktop-*-jar-with-dependencies.jar
#   --forge-jar PATH       explicit Forge jar (overrides MAKE_MAGIC_FORGE_HOME glob)
#
# Compiled with --release 17 (Forge 2.0.13's baseline; bytecode runs on JRE 21).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

JAVAC="${JAVAC:-/opt/homebrew/opt/openjdk@17/bin/javac}"
FORGE_JAR=""
FORGE_JAR_GLOB="forge-gui-desktop-*-jar-with-dependencies.jar"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --forge-jar) FORGE_JAR="$2"; shift 2;;
        --javac) JAVAC="$2"; shift 2;;
        *) echo "Unknown arg: $1" >&2; exit 2;;
    esac
done

if [[ -z "$FORGE_JAR" ]]; then
    if [[ -z "${MAKE_MAGIC_FORGE_HOME:-}" ]]; then
        echo "ERROR: set MAKE_MAGIC_FORGE_HOME or pass --forge-jar <path>" >&2
        exit 2
    fi
    # shellcheck disable=SC2086
    matches=( $MAKE_MAGIC_FORGE_HOME/$FORGE_JAR_GLOB )
    if [[ ! -f "${matches[0]}" ]]; then
        echo "ERROR: no $FORGE_JAR_GLOB under $MAKE_MAGIC_FORGE_HOME" >&2
        exit 2
    fi
    FORGE_JAR="${matches[0]}"
fi

if [[ ! -f "$FORGE_JAR" ]]; then
    echo "ERROR: Forge jar not found: $FORGE_JAR" >&2
    exit 2
fi
if [[ ! -x "$JAVAC" ]]; then
    echo "ERROR: javac not executable: $JAVAC" >&2
    exit 2
fi

# The Homebrew openjdk@17 `javac`/`jar` are launcher stubs that need a JAVA_HOME
# pointing at the real JDK (otherwise: "Unable to locate a Java Runtime").
# Derive it from the javac path unless the caller already set one.
if [[ -z "${JAVA_HOME:-}" ]]; then
    # Resolve the (possibly relative) javac symlink to an absolute path.
    javac_dir="$(cd "$(dirname "$JAVAC")" && pwd)"
    target="$(readlink "$JAVAC" 2>/dev/null || true)"
    if [[ -n "$target" ]]; then
        case "$target" in
            /*) real_javac="$target";;
            *)  real_javac="$(cd "$javac_dir" && cd "$(dirname "$target")" && pwd)/$(basename "$target")";;
        esac
    else
        real_javac="$javac_dir/$(basename "$JAVAC")"
    fi
    cand="$(dirname "$(dirname "$real_javac")")"   # .../Contents/Home
    if [[ -x "$cand/bin/javac" ]]; then
        export JAVA_HOME="$cand"
    fi
fi
JAR_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}jar"
command -v "$JAR_BIN" >/dev/null 2>&1 || JAR_BIN="jar"

echo "java home: ${JAVA_HOME:-<unset>}"
echo "javac    : $JAVAC"
echo "forge jar: $FORGE_JAR"

rm -rf build dist
mkdir -p build dist

# Collect all sources (harness + shadow patch).
SOURCES=()
while IFS= read -r f; do SOURCES+=("$f"); done < <(find src patch -name '*.java' | sort)
echo "sources  : ${#SOURCES[@]} file(s)"
printf '  %s\n' "${SOURCES[@]}"

"$JAVAC" --release 17 -cp "$FORGE_JAR" -d build "${SOURCES[@]}"

"$JAR_BIN" cfe dist/make-magic-forge-simai.jar org.makemagic.simai.SimAIMatch -C build .

echo "built    : dist/make-magic-forge-simai.jar"
"$JAR_BIN" tf dist/make-magic-forge-simai.jar | grep -E 'SimAIMatch|StaticAbilityContinuous' || true
