#!/bin/sh
# session-start.sh — SessionStart hook for the make-magic plugin.
#
# Runs on every session start/resume/clear/compact. It:
#   1. Ensures uv is provisioned (via ensure-uv.sh) — fail-open.
#   2. Prepends the uv cache dir to PATH for the rest of the session by
#      appending an `export PATH=...` line to the file named by $CLAUDE_ENV_FILE.
#
# This makes the skills' existing `uv run --script ...` calls resolve uv without
# changing any skill. This hook is plain POSIX sh with NO uv/python dependency,
# because it must run before uv exists.

set -u

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"

# Provision uv if needed; capture the resolved cache/bin dir it prints on stdout.
UV_DIR="$(sh "$PLUGIN_ROOT/scripts/ensure-uv.sh" 2>/dev/null || true)"

# Fall back to the documented default cache dir if the script printed nothing.
if [ -z "${UV_DIR:-}" ]; then
  UV_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/claude-plugins/uv"
fi

# Prepend the uv dir to PATH for all subsequent Bash commands in this session.
# Guard against duplicate lines: multiple SessionStart matchers (startup/resume/
# clear/compact) can fire in one session, and appending unconditionally would add
# the same export line each time. Only append if it isn't already present.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  PATH_LINE="$(printf 'export PATH="%s:$PATH"' "$UV_DIR")"
  if [ ! -f "$CLAUDE_ENV_FILE" ] || ! grep -qF "$PATH_LINE" "$CLAUDE_ENV_FILE" 2>/dev/null; then
    printf '%s\n' "$PATH_LINE" >> "$CLAUDE_ENV_FILE"
  fi
fi

exit 0
