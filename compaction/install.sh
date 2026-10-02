#!/usr/bin/env bash
# Install BG-BGI/fast-jev-compaction (fork of tamaratran/fast-jev-compaction), a Claude Code plugin with
# two modes: "tool" trims long tool results as they arrive, "session" replaces
# the compaction summary and auto-compacts past a token threshold.
#
# READ compaction/README.md BEFORE running this. It sends far more off the
# machine than any other component in this repository: tool output (tool
# mode) or up to about 25,000 tokens of conversation state per request
# (session mode) to TypeSafe. The fork runs a pattern redactor first
# (src/redact.ts).
set -uo pipefail

MARKETPLACE="BG-BGI/fast-jev-compaction"
PLUGIN_ID="fast-jev-compaction@fast-jev-compaction"
MIN_VERSION="2.1.274"
MODE="${COMPACTION_MODE:-tool}"
case "$MODE" in tool|session|both) ;; *) echo "compaction: COMPACTION_MODE must be tool, session or both (got: $MODE)" >&2; exit 2 ;; esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat >&2 <<EOF
usage: [COMPACTION_MODE=tool|session|both] $0

Requires:
  - Claude Code >= $MIN_VERSION with function hooks enabled
    (env CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 in the account's settings --
    this script checks and prints the edit; it does not write settings.json)
  - a loadable TYPESAFE_API_KEY (env, or the key file named by
    AIRLOCK_KEY_FILE)

This installs a marketplace and a plugin under whichever Claude account
CLAUDE_CONFIG_DIR (or the default ~/.claude) points at. Run it once per
account that should have compaction.
EOF
  exit 2
}
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && usage

command -v claude >/dev/null 2>&1 || { echo "compaction: 'claude' not found on PATH" >&2; exit 1; }

# --- version check -----------------------------------------------------------
CUR_VERSION="$(claude --version 2>/dev/null | awk '{print $1}')"
if [ -z "$CUR_VERSION" ]; then
  echo "compaction: could not read the Claude Code version" >&2
  exit 1
fi
ver_ge() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ]; }
if ! ver_ge "$CUR_VERSION" "$MIN_VERSION"; then
  echo "compaction: this Claude Code is $CUR_VERSION; fast-jev-compaction needs >= $MIN_VERSION." >&2
  echo "  Upgrade first (see claude-update/, or 'npm install -g @anthropic-ai/claude-code')." >&2
  exit 1
fi
echo "compaction: Claude Code $CUR_VERSION >= $MIN_VERSION, ok"

# --- function hooks: print the settings edit, do not make it ---------------
ACCOUNT_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SETTINGS="$ACCOUNT_DIR/settings.json"
HOOKS_ENABLED=0
if [ -f "$SETTINGS" ] && grep -q '"CLAUDE_CODE_ENABLE_FUNCTION_HOOKS" *: *"1"' "$SETTINGS" 2>/dev/null; then
  HOOKS_ENABLED=1
fi
if [ "$HOOKS_ENABLED" = "1" ]; then
  echo "compaction: CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 already set in $SETTINGS"
else
  cat <<EOF

compaction: function hooks are not (verifiably) enabled in $SETTINGS.
Add this to that file's "env" block yourself -- this script does not edit
settings.json:

    "env": {
      "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "1"
    }

Then re-run this script.
EOF
  exit 1
fi

# --- the key: read into a shell variable, never echoed ---------------------
# Key-file resolution: the ONE shell implementation, shared with every other
# component here. The order and the pointer-file trust rules are documented in
# airlock/keyfile.py's module docstring. Fails open if the helper is missing.
if [ -r "$SCRIPT_DIR/../install/keyfile.sh" ]; then
  . "$SCRIPT_DIR/../install/keyfile.sh"
else
  airlock_key_file() {
    local f="${AIRLOCK_KEY_FILE:-${JEVKIT_KEY_FILE:-}}"
    if [ -n "$f" ]; then printf '%s\n' "$f"; return 0; fi
    [ -r "$HOME/.config/jev-kit/env" ] && { printf '%s\n' "$HOME/.config/jev-kit/env"; return 0; }
    [ -r "$HOME/.config/airlock/env" ] && { printf '%s\n' "$HOME/.config/airlock/env"; return 0; }
    printf '%s\n' "$HOME/.config/jev-kit/env"
  }
fi
if [ -z "${TYPESAFE_API_KEY:-}" ]; then
  KEY_FILE="$(airlock_key_file)"
  if [ -r "$KEY_FILE" ]; then
    TYPESAFE_API_KEY="$(sed -n 's/^ *\(export \)\?TYPESAFE_API_KEY=//p' "$KEY_FILE" | head -1)"
  fi
fi
if [ -z "${TYPESAFE_API_KEY:-}" ]; then
  echo "compaction: no TYPESAFE_API_KEY in the environment or the key file." >&2
  echo "  Load it first:  set -a; . ~/.config/jev-kit/env 2>/dev/null; set +a" >&2
  exit 1
fi
echo "compaction: key loaded (not printed); the plugin reads it from the environment or the key file, so it is not stored in plugin config"

# --- marketplace and plugin --------------------------------------------------
echo "compaction: claude plugin marketplace add $MARKETPLACE"
claude plugin marketplace add "$MARKETPLACE"

echo "compaction: claude plugin install $PLUGIN_ID"
claude plugin install "$PLUGIN_ID" --config "mode=$MODE"

cat <<'EOF'

Installed. Before you rely on this, re-read compaction/README.md: this
plugin sends tool output (tool mode) or up to ~25,000 tokens of conversation
state per request (session mode) to TypeSafe, after a pattern redactor (keys,
tokens, emails, secrets in KEY=value). Pattern matching is not a guarantee:
prose, names and paths still go out.
EOF
