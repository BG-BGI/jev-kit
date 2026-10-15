#!/usr/bin/env bash
# Check that the `knowledge` MCP server runs here, then register it with Claude
# Code, so Claude can log what it learned (graph_log) and look it up
# (graph_search). The rule for WHEN to log ships inside the server (its
# `initialize` reply carries `instructions`), so registering is the whole job.
#
# The server is a module (`python3 -m knowledge.server`), so the registered
# command runs it under `env PYTHONPATH=<root>`. <root> is the deployed release
# when it has knowledge/ (stable across checkout moves), else this checkout.
#
# Registration goes through `claude mcp add --scope user`, never by editing
# ~/.claude.json by hand. It is idempotent: a server already registered at this
# root is left alone. Undo with `claude mcp remove --scope user jev-knowledge`.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
AIRLOCK_HOME="${AIRLOCK_HOME:-$HOME/.local/share/airlock}"
NAME="jev-knowledge"
PY="${PYTHON:-python3}"
PRINT_ONLY=0

usage() {
  cat >&2 <<USAGE
usage: $0 [--print-only]

Runs the server through a real MCP handshake (it must answer initialize with
its instructions, and list the 'graph_log' tool), then registers it:

    claude mcp add --scope user $NAME -- env PYTHONPATH=<root> <python> -m knowledge.server

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

command -v "$PY" >/dev/null 2>&1 || { echo "knowledge: '$PY' not found" >&2; exit 1; }
PY_ABS="$(command -v "$PY")"

ROOT="$AIRLOCK_HOME/current"
[ -f "$ROOT/knowledge/server.py" ] || ROOT="$REPO_ROOT"
[ -f "$ROOT/knowledge/server.py" ] || { echo "knowledge: no server under $ROOT" >&2; exit 1; }
echo "knowledge: server root $ROOT"

HANDSHAKE="$(printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"knowledge-install","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | (cd "$ROOT" && PYTHONPATH="$ROOT" "$PY_ABS" -m knowledge.server 2>/dev/null))"
if ! printf '%s' "$HANDSHAKE" | grep -q '"name": "graph_log"'; then
  echo "knowledge: the server did not answer the MCP handshake. Try it by hand:" >&2
  echo "      cd $ROOT && PYTHONPATH=$ROOT $PY_ABS -m knowledge.server" >&2
  exit 1
fi
if ! printf '%s' "$HANDSHAKE" | grep -q '"instructions"'; then
  echo "knowledge: the server answered, but sent no instructions, so Claude would" >&2
  echo "  have the tool and no rule for when to log. Update this checkout." >&2
  exit 1
fi
echo "knowledge: handshake ok (graph_log listed, instructions sent)"

ADD=(claude mcp add --scope user "$NAME" -- env "PYTHONPATH=$ROOT" "$PY_ABS" -m knowledge.server)
if [ "$PRINT_ONLY" = "1" ] || ! command -v claude >/dev/null 2>&1; then
  [ "$PRINT_ONLY" = "1" ] || echo "knowledge: 'claude' is not on PATH, so nothing was registered."
  echo "Register it with:"
  echo "    ${ADD[*]}"
  exit 0
fi

if CURRENT="$(claude mcp get "$NAME" 2>/dev/null)"; then
  if printf '%s' "$CURRENT" | grep -qF -- "PYTHONPATH=$ROOT"; then
    echo "knowledge: already registered at this root; nothing to do"
    exit 0
  fi
  echo "knowledge: '$NAME' is already registered, at a different root. Left as is." >&2
  echo "  To point it here:" >&2
  echo "      claude mcp remove --scope user $NAME && ${ADD[*]}" >&2
  exit 0
fi

if "${ADD[@]}" >/dev/null 2>&1; then
  echo "knowledge: registered '$NAME' for your user. Restart Claude Code sessions to pick it up."
  echo "  Undo: claude mcp remove --scope user $NAME"
else
  echo "knowledge: 'claude mcp add' failed. Run it yourself:" >&2
  echo "      ${ADD[*]}" >&2
  exit 1
fi
