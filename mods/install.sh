#!/usr/bin/env bash
# Register the jev-kit plugin marketplace with Claude Code and install the
# jev-stats pane plugin from it. The marketplace manifest
# (.claude-plugin/marketplace.json) ships in every release, but until this
# script nothing installed the plugin: the jev-kit CLI's `plugins` command
# only UPDATES plugins that are already there.
#
# Everything goes through `claude plugin ...`, the supported way, never by
# editing settings.json or installed_plugins.json by hand. Idempotent: a
# marketplace or plugin that is already registered is left alone.
# Undo:
#     claude plugin uninstall jev-stats@jev-kit
#     claude plugin marketplace remove jev-kit
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
AIRLOCK_HOME="${AIRLOCK_HOME:-$HOME/.local/share/airlock}"
MARKETPLACE="jev-kit"
PLUGIN="jev-stats@jev-kit"
PRINT_ONLY=0

usage() {
  cat >&2 <<USAGE
usage: $0 [--print-only]

Registers the jev-kit plugin marketplace and installs the jev-stats plugin:

    claude plugin marketplace add <release or checkout>
    claude plugin install $PLUGIN

--print-only   print those commands; register and install nothing
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

# Prefer the deployed release (stable across checkout moves, swapped
# atomically by an update); fall back to this checkout.
ROOT="$AIRLOCK_HOME/current"
[ -f "$ROOT/.claude-plugin/marketplace.json" ] || ROOT="$REPO_ROOT"
if [ ! -f "$ROOT/.claude-plugin/marketplace.json" ]; then
  echo "stats: no .claude-plugin/marketplace.json at $ROOT" >&2
  exit 1
fi
echo "stats: marketplace manifest at $ROOT"

if [ "$PRINT_ONLY" = "1" ] || ! command -v claude >/dev/null 2>&1; then
  [ "$PRINT_ONLY" = "1" ] || echo "stats: 'claude' is not on PATH, so nothing was installed."
  echo "Install it with:"
  echo "    claude plugin marketplace add $ROOT"
  echo "    claude plugin install $PLUGIN"
  exit 0
fi

if claude plugin marketplace list 2>/dev/null | grep -qw "$MARKETPLACE"; then
  echo "stats: marketplace '$MARKETPLACE' already registered; left as is"
elif claude plugin marketplace add "$ROOT" >/dev/null 2>&1; then
  echo "stats: registered marketplace '$MARKETPLACE' from $ROOT"
else
  echo "stats: 'claude plugin marketplace add $ROOT' failed. Run it yourself." >&2
  exit 1
fi

if claude plugin list 2>/dev/null | grep -qw "$PLUGIN"; then
  echo "stats: $PLUGIN already installed; left as is"
elif claude plugin install "$PLUGIN" >/dev/null 2>&1; then
  echo "stats: installed $PLUGIN. Run /reload-plugins in Claude Code (or restart) to pick it up."
  echo "  Undo: claude plugin uninstall $PLUGIN"
else
  echo "stats: 'claude plugin install $PLUGIN' failed. Run it yourself:" >&2
  echo "    claude plugin install $PLUGIN" >&2
  exit 1
fi
