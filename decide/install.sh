#!/usr/bin/env bash
# Check that the `decide` MCP server runs here, then register it with Claude
# Code, so Claude can ask Jev to make bulk decisions without anyone editing a
# CLAUDE.md. The rule for WHEN to call it ships inside the server (its
# `initialize` reply carries `instructions`), so registering is the whole job.
#
# Registration goes through `claude mcp add --scope user`, the supported way,
# never by editing ~/.claude.json by hand. It is idempotent: a server already
# registered at this path is left alone. Undo with `claude mcp remove jev-decide`.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
AIRLOCK_HOME="${AIRLOCK_HOME:-$HOME/.local/share/airlock}"
NAME="jev-decide"
PY="${PYTHON:-python3}"
PRINT_ONLY=0

usage() {
  cat >&2 <<USAGE
usage: $0 [--print-only]

Runs the server through a real MCP handshake (it must answer initialize with
its instructions, and list the 'decide' tool), then registers it:

    claude mcp add --scope user $NAME -- <python> <server.py>

--print-only   do the handshake and print that command; register nothing
USAGE
  exit 2
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --print-only) PRINT_ONLY=1; shift ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

command -v "$PY" >/dev/null 2>&1 || { echo "decide: '$PY' not found" >&2; exit 1; }
PY_ABS="$(command -v "$PY")"

# Prefer the deployed release (stable across checkout moves, swapped
# atomically by an update); fall back to this checkout.
SERVER="$AIRLOCK_HOME/current/decide/server.py"
[ -f "$SERVER" ] || SERVER="$SCRIPT_DIR/server.py"
[ -f "$SERVER" ] || { echo "decide: no server at $SERVER" >&2; exit 1; }
echo "decide: server at $SERVER"

HANDSHAKE="$(printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"decide-install","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | "$PY_ABS" "$SERVER" 2>/dev/null)"
if ! printf '%s' "$HANDSHAKE" | grep -q '"name": "decide"'; then
  echo "decide: the server did not answer the MCP handshake. Try it by hand:" >&2
  echo "      $PY_ABS $SERVER" >&2
  exit 1
fi
if ! printf '%s' "$HANDSHAKE" | grep -q '"instructions"'; then
  echo "decide: the server answered, but sent no instructions, so Claude would" >&2
  echo "  have the tool and no rule for when to use it. Update this checkout." >&2
  exit 1
fi
echo "decide: handshake ok (tool listed, instructions sent)"

ADD=(claude mcp add --scope user "$NAME" -- "$PY_ABS" "$SERVER")
if [ "$PRINT_ONLY" = "1" ] || ! command -v claude >/dev/null 2>&1; then
  [ "$PRINT_ONLY" = "1" ] || echo "decide: 'claude' is not on PATH, so nothing was registered."
  echo "Register it with:"
  echo "    ${ADD[*]}"
  exit 0
fi

if CURRENT="$(claude mcp get "$NAME" 2>/dev/null)"; then
  if printf '%s' "$CURRENT" | grep -qF -- "$SERVER"; then
    echo "decide: already registered at this path; nothing to do"
    exit 0
  fi
  echo "decide: '$NAME' is already registered, at a different path. Left as is." >&2
  echo "  To point it here:" >&2
  echo "      claude mcp remove --scope user $NAME && ${ADD[*]}" >&2
  exit 0
fi

if "${ADD[@]}" >/dev/null 2>&1; then
  echo "decide: registered '$NAME' for your user. Restart Claude Code sessions to pick it up."
  echo "  Undo: claude mcp remove --scope user $NAME"
else
  echo "decide: 'claude mcp add' failed. Run it yourself:" >&2
  echo "      ${ADD[*]}" >&2
  exit 1
fi
