# Installing jev-kit on macOS

The install is the same bash installer as Linux. It notices it is on macOS and
changes three things.

| Linux | macOS |
|---|---|
| systemd user units in `~/.config/systemd/user` | LaunchAgents in `~/Library/LaunchAgents`, labelled `com.jev-kit.*` |
| daemon socket in `$XDG_RUNTIME_DIR/airlock` | `~/Library/Caches/airlock/airlock.sock` (dir 700, socket 600) |
| `plocate` index of `$HOME`, rebuilt hourly | Spotlight (`mdfind`), which the OS keeps current |

The `.service` and `.timer` files are still the one description of what runs.
`install/launchd.py` reads them and writes the matching property list, so
there is no second copy to keep in step.

## 1. Prerequisites

- Python 3.10 or newer. `/usr/bin/python3` from the Command Line Tools is
  often older, so install one with Homebrew (`brew install python`). The
  installer picks the first `python3` on `PATH` that is new enough, and the
  LaunchAgents use that same interpreter.
- `git`. Node and `uv` only matter for `--browser` and `--review`.
- Spotlight indexing on for the data volume. Check with
  `mdutil -s /System/Volumes/Data`.

## 2. Install

```bash
git clone https://github.com/BG-BGI/jev-kit.git ~/code/jev-kit
cd ~/code/jev-kit
install/install.sh --check-only      # the plan, nothing installed
install/install.sh                   # the default set
```

Then put the key in `~/.config/jev-kit/env`, exactly as the
[Quickstart](../README.md#quickstart) says. Never print it.

## 3. Check it

```bash
install/doctor.sh
launchctl print gui/$(id -u)/com.jev-kit.airlock-daemon | head -20
tail ~/Library/Logs/jev-kit/airlock-daemon.log
```

Without a key the daemon exits at once and `doctor.sh` reports it as not
running. That is correct: the guard still fails open, and the client makes a
direct HTTPS call once a key exists.

## 4. File search

There is no index to build. When an agent crawls `$HOME` by name, the guard
steers it at:

```bash
mdfind -onlyin <dir> -name '<pattern>' 2>/dev/null
```

Spotlight skips hidden directories such as `~/.config` and `.git`. A search
rooted in one of those is left to `find`, because steering it at `mdfind`
would report the files as absent.

## 5. Remove a LaunchAgent

```bash
python3 install/launchd.py remove airlock-daemon
python3 install/launchd.py remove airlock-health
```

## What was tested

macOS 26.6 on Apple silicon, Homebrew Python 3.14, bash 3.2 as `/bin/bash`.
All unit tests pass. A real install ran with `--guard --daemon --monitoring
--filesearch`. `doctor.sh` passed every check except the daemon, which exits
without a key, as section 3 says. With a dummy key the daemon answered a
ping on its socket; a piped event got a real R5 deny, and `ls` got a clean
allow. Nobody has run the Jev-judged path on macOS yet, since that needs a key.
