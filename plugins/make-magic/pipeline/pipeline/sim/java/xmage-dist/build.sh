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
#   i.e. a magefree/mage @ xmage_1.4.60V3 clone where the in-search driver SEAM PATCHES
#   (patches/NNNN-*.patch, see below) have been applied and then
#   `mvn -pl Mage.Tests,Mage.Server.Plugins/Mage.Player.AI.MA,Mage.Server.Plugins/Mage.Game.CommanderDuel -am install -DskipTests`
#   has run. In CI (xmage-dist-release.yml) that clone + patch-apply + install is the
#   preceding step.
#
# SEAM PATCHES (in-search quad Driver): the dist bundles the engine + harness + the quad
#   seam. The seam is a committed, reviewable patch series over the pinned upstream tag
#   (there is NO make-magic fork). Apply it to the reactor clone BEFORE `mvn install` by
#   pointing XMAGE_SRC at the clone dir and running this script (or the standalone
#   `--apply-patches <dir>` mode); a `git apply --check` guard FAILS LOUD if a patch stops
#   applying on an XMage bump.
#
# Toolchain (no reliance on PATH):
#   JAVA_HOME  a JDK 17 (default: derived from openjdk@17 if present)
#   MVN        maven to use (default: mvn on PATH)
#
# Env:
#   XMAGE_SRC  (optional) path to the reactor clone; if set, the seam patches are applied
#              to it (with a --check guard) before the shade package step.
#
# Flags:
#   --apply-patches <dir>   apply the seam patch series to <dir> (with --check guard) and exit.
#   --print-sha             after building, print the jar's SHA256 (copy into xmage_runtime's pin).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# --- seam patch application (shared by local + CI) ---------------------------------
# Apply patches/NNNN-*.patch to a pristine reactor clone. Fails loud via `git apply
# --check` so an XMage bump that breaks a hunk stops the build instead of silently
# shipping an unseamed dist.
apply_seam_patches() {
    local src="$1"
    local patches=("$HERE"/patches/*.patch)
    [[ -e "${patches[0]}" ]] || { echo "ERROR: no seam patches found under $HERE/patches" >&2; exit 1; }
    echo ">> applying ${#patches[@]} seam patch(es) to $src"
    ( cd "$src" && git apply --check "${patches[@]}" ) \
        || { echo "ERROR: seam patches do NOT apply cleanly to $src (XMage bump? re-derive patches/)" >&2; exit 1; }
    ( cd "$src" && git apply "${patches[@]}" )
    echo ">> seam patches applied."
}

if [[ "${1:-}" == "--apply-patches" ]]; then
    [[ -n "${2:-}" ]] || { echo "usage: build.sh --apply-patches <reactor-clone-dir>" >&2; exit 1; }
    apply_seam_patches "$2"
    exit 0
fi

if [[ -n "${XMAGE_SRC:-}" ]]; then
    apply_seam_patches "$XMAGE_SRC"
fi

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
