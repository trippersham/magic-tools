#!/bin/sh
# ensure-uv.sh — minimal *nix-only (macOS/Linux) bootstrap that guarantees `uv`
# is available so the plugin's `uv run --script ...` calls work on machines/cloud
# sessions where uv isn't pre-installed.
#
# Contract:
#   - If uv is already on PATH, or a cached uv exists, do nothing.
#   - Otherwise download the PINNED uv static binary from astral's official
#     GitHub release host into the cache dir.
#   - FAIL-OPEN: any error prints a short note to stderr and exits 0 so a
#     SessionStart hook never blocks the session.
#   - On success, print the cache dir (containing `uv`) on stdout so a caller
#     can prepend it to PATH. Cache dir: ${XDG_CACHE_HOME:-$HOME/.cache}/claude-plugins/uv
#
# Public method only: this uses astral's official pinned release tarballs. It
# does not copy or depend on any internal/proprietary tooling.

set -eu

# --- PINNED uv version -------------------------------------------------------
# Bump this single line to upgrade uv (see https://github.com/astral-sh/uv/releases).
UV_VERSION="0.11.32"
# -----------------------------------------------------------------------------

CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/claude-plugins/uv"
UV_BIN="$CACHE_DIR/uv"

# fail-open helper: note to stderr, still emit best-known cache dir, exit 0.
bail() {
  printf 'ensure-uv: %s (continuing without provisioning uv)\n' "$1" >&2
  printf '%s\n' "$CACHE_DIR"
  exit 0
}

# 1. uv already on PATH → nothing to do.
if command -v uv >/dev/null 2>&1; then
  command -v uv | sed 's#/uv$##'
  exit 0
fi

# 2. Cached binary already present and executable → done.
if [ -x "$UV_BIN" ]; then
  printf '%s\n' "$CACHE_DIR"
  exit 0
fi

# 3. Download the pinned static binary. Detect OS + arch for the release triple.
os="$(uname -s)"
arch="$(uname -m)"
case "$os" in
  Darwin) plat="apple-darwin" ;;
  Linux)  plat="unknown-linux-gnu" ;;
  *)      bail "unsupported OS '$os' (macOS/Linux only)" ;;
esac
case "$arch" in
  arm64|aarch64) cpu="aarch64" ;;
  x86_64|amd64)  cpu="x86_64" ;;
  *)             bail "unsupported arch '$arch'" ;;
esac
triple="${cpu}-${plat}"
asset="uv-${triple}.tar.gz"
url="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${asset}"

# Pick a downloader.
if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -qO "$2" "$1"; }
else
  bail "neither curl nor wget available"
fi

# Pick a sha256 tool (macOS: shasum -a 256; Linux: sha256sum). Prints the hex
# digest of "$1" on stdout.
if command -v sha256sum >/dev/null 2>&1; then
  sha256_of() { sha256sum "$1" | awk '{print $1}'; }
elif command -v shasum >/dev/null 2>&1; then
  sha256_of() { shasum -a 256 "$1" | awk '{print $1}'; }
else
  bail "no sha256 tool (sha256sum/shasum) available"
fi

mkdir -p "$CACHE_DIR" || bail "cannot create cache dir $CACHE_DIR"

# 4. mkdir-based lock to avoid concurrent double-install.
lock="$CACHE_DIR/.lock"
if ! mkdir "$lock" 2>/dev/null; then
  # Another process is installing. Wait briefly for it to finish.
  i=0
  while [ ! -x "$UV_BIN" ] && [ "$i" -lt 60 ]; do
    i=$((i + 1))
    sleep 1
  done
  [ -x "$UV_BIN" ] && { printf '%s\n' "$CACHE_DIR"; exit 0; }
  bail "timed out waiting for concurrent install"
fi
# Always release the lock on exit.
trap 'rmdir "$lock" 2>/dev/null || true' EXIT INT TERM

tmp="$(mktemp -d "${TMPDIR:-/tmp}/ensure-uv.XXXXXX")" || bail "mktemp failed"
tarball="$tmp/$asset"

fetch "$url" "$tarball" || { rm -rf "$tmp"; bail "download failed from $url"; }

# Supply-chain integrity check: verify the tarball against astral's published
# SHA256 for this exact asset BEFORE extracting/exec'ing anything. If the
# checksum can't be fetched or the digests don't match, we fail-open: no
# extraction, no unverified binary is ever run (see bail()).
sumfile="$tmp/$asset.sha256"
fetch "$url.sha256" "$sumfile" || { rm -rf "$tmp"; bail "could not fetch checksum for $asset (not verifying → not installing)"; }
expected="$(awk '{print $1}' "$sumfile" 2>/dev/null | head -n 1)"
actual="$(sha256_of "$tarball")"
if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
  rm -rf "$tmp"
  bail "checksum mismatch for $asset (expected '$expected', got '$actual') → refusing to run unverified binary"
fi

# The tarball extracts to a dir named after the triple, containing the `uv` binary.
tar -xzf "$tarball" -C "$tmp" || { rm -rf "$tmp"; bail "extract failed"; }

extracted="$(find "$tmp" -type f -name uv -perm -u+x 2>/dev/null | head -n 1)"
[ -n "$extracted" ] || extracted="$tmp/uv-${triple}/uv"
[ -f "$extracted" ] || { rm -rf "$tmp"; bail "uv binary not found in archive"; }

# Atomic-ish install: move into place, then verify.
cp "$extracted" "$UV_BIN.tmp" || { rm -rf "$tmp"; bail "copy into cache failed"; }
chmod +x "$UV_BIN.tmp" || true
mv "$UV_BIN.tmp" "$UV_BIN" || { rm -rf "$tmp"; bail "install into cache failed"; }
rm -rf "$tmp"

if "$UV_BIN" --version >/dev/null 2>&1; then
  printf '%s\n' "$CACHE_DIR"
  exit 0
fi

bail "downloaded uv failed to run"
