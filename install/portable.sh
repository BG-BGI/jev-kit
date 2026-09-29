#!/usr/bin/env bash
# GNU/BSD shims for the installer scripts. Source it; do not run it.
#
#     . "$(dirname "${BASH_SOURCE[0]}")/../install/portable.sh"
#
# The kit was written against GNU coreutils and bash 4+. macOS ships BSD
# userland and bash 3.2, so every place the two disagree goes through one of
# these functions instead of calling the GNU spelling directly:
#
#   airlock_replace_link TMP DEST   atomic `mv -T` (GNU) / `mv -fh` (BSD):
#                                   rename a new symlink over an old one
#                                   without following the old one into the
#                                   directory it points at
#   airlock_stat_uid_mode PATH      "<uid> <octal mode>" (stat -c / stat -f)
#   airlock_timeout SECS CMD...     timeout(1), gtimeout(1), or a perl alarm
#   airlock_is_macos                true on Darwin
#
# Bash 3.2 has no `mapfile`; callers read lines with a `while read` loop.

airlock_is_macos() {
  [ "$(uname -s 2>/dev/null)" = "Darwin" ]
}

airlock_replace_link() {
  if mv --version >/dev/null 2>&1; then
    mv -T "$1" "$2"
  else
    mv -fh "$1" "$2"
  fi
}

airlock_stat_uid_mode() {
  stat -c '%u %a' "$1" 2>/dev/null || stat -f '%u %Lp' "$1" 2>/dev/null
}

airlock_timeout() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$secs" "$@"
  else
    perl -e 'alarm shift; exec @ARGV or exit 127' "$secs" "$@"
  fi
}
