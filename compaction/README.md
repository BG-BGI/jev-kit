# compaction: fast-jev-compaction (optional, off by default, read this first)

[BG-BGI/fast-jev-compaction](https://github.com/BG-BGI/fast-jev-compaction),
a fork of [tamaratran/fast-jev-compaction](https://github.com/tamaratran/fast-jev-compaction),
is a Claude Code plugin that uses TypeSafe's Jev model to decide what stays in
context. It has two modes, chosen at install time with `COMPACTION_MODE`:

| Mode | Hooks | What it does | What it sends |
|---|---|---|---|
| `tool` (default) | `tool.call` | Trims long `Bash` and MCP results (and `Read`, if you turn on `compactRead`) as they arrive. Line ranges Jev says are not needed become a one-line note; everything kept is verbatim. | One tool output at a time, up to `maxStateTokens` (20,000), plus your last few prompts as the goal. |
| `session` | `session.compact`, `turn.complete` | Replaces the built-in compaction summary. Jev decides per tool call whether the call and its result stay. Auto-compacts past 60% of the context window. | Up to **25,000 tokens** of conversation state (tool inputs, tool results, text) per request. |
| `both` | all three | Trims results on arrival, and runs the session compactor at the threshold. | Both of the above. |

Every mode fails open: a Jev error, a missing key, or a rewrite that saves
under 25% leaves the original result (or the built-in summary) in place.

## What it sends off the machine

**This is the largest data egress of anything in this repository**, in either
mode. The BG-BGI fork runs a pattern redactor (`src/redact.ts`, ported from
`airlock/redact.py`: keys, tokens, JWTs, emails, `KEY=value` secrets, private
keys, long hex/base64) over everything before it leaves. That is pattern
matching only. Document text, email bodies, prose, names and paths still go to
TypeSafe, as the contents of `Read`/`Bash`/MCP output.

`tool` mode sends less than `session` mode: it never sends tool inputs or the
rest of the history, only the output being judged and the goal. But it fires
on every long result, so over a session it can send more requests.

Compare that with `belay/`, which sends only the task text, the final
message and check command lines, through a 13-rule redactor, capped at a few
thousand characters.

## Why it might still be worth it

Both modes are fast. Measured once on this box on 2026-09-19 with the earlier
`session`-only plugin, a manual `/compact` took a 49,288-token session down to
23,111 tokens in 906 ms. `tool` mode has no measurement here yet.

**The decision taken here was to ship the installer but never to turn it on.**
The exposure above is why. This component exists so that a machine which has
made its own decision about the trade-off can install it, not so that it
becomes a default.

It is **off by default** everywhere in this repository. `install/install.sh`
never turns it on without `--compaction`, and even then only prints the
warning; you run `compaction/install.sh` yourself.

## Recall: getting the cut text back

In `tool` mode the plugin keeps what it cuts. Before it drops a line range, it
pipes the range to `python3 -m airlock.recall put`, which stores it in
`recall.db` in the airlock state directory (mode 600, in a 700 directory). The
omission note then names a ref:

```
[fast-jev-compaction omitted Bash output lines 21-440 (420 lines); recall ref=r_ab12cd34 range=21-440 to restore, or re-run with a narrower command]
```

The plugin also registers a tool, `mcp__fast-jev-compaction__recall`, taking
`ref`, an optional `range` and an optional `maxChars`. The model calls it only
when it needs the text, so the text stays out of context until then. Each line
comes back prefixed with its original line number.

Nothing here leaves the machine. Rows expire after 7 days and the table is held
under 50 MB, both enforced on every write. An expired or unknown ref returns a
message that says to re-run the command.

The store keeps the cut text as it was. It is **not** redacted, because a
recall that returned altered text would be wrong. The 600 mode, the TTL and the
size cap are the protection. Anything the redactor would have caught is on
disk for up to a week.

Recall needs the jev-kit `airlock` package (`~/.local/share/airlock/current`, or
`$AIRLOCK_HOME/current`) and `python3`. Without them the plugin keeps the old
note and the output is compacted as before.

| Option | Default | Meaning |
|---|---|---|
| `recallEnabled` | `true` | `false` turns off storing and the tool. |
| `recallTtlDays` | `7` | Age at which stored text is deleted. |
| `recallMaxMB` | `50` | Oldest refs are deleted past this size. |
| `recallMaxChars` | `20000` | Most characters one recall call returns. |

Manage the store by hand with `python3 -m airlock.recall list`, `show REF`,
`prune` and `purge`. `purge` deletes everything. The model is not told to touch
the database, and `R12-self-tamper` still blocks a Bash call that tries.

Allow the tool once in `settings.json` (`permissions.allow`:
`mcp__fast-jev-compaction__recall`) so a recall does not prompt each time.

## Requirements

- Claude Code **2.1.274 or later**, with function hooks enabled
  (`CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` in the account's `settings.json`
  `env` block).
- A loadable `TYPESAFE_API_KEY`: the environment, `settings.json` `env`, or
  `~/.config/jev-kit/env`. The plugin reads the same kit file every other
  component reads, so the key is not copied into plugin config.

## Install

```bash
set -a; . ~/.config/jev-kit/env 2>/dev/null; set +a   # loads the key into this shell only
compaction/install.sh                                  # tool mode
COMPACTION_MODE=session compaction/install.sh          # or session / both
```

`install.sh`:

1. checks the Claude Code version;
2. checks whether `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` is already set in the
   account's `settings.json`, and if not, **prints** the edit and stops. It
   does not write settings.json itself;
3. checks that a key is loadable (never echoed, never logged);
4. clones `BG-BGI/fast-jev-compaction` at a pinned full-SHA commit into
   `~/.local/share/jev-compaction/releases/<sha>` and runs
   `claude plugin marketplace add` on that local release, so the install never
   tracks the repo's HEAD;
5. runs `claude plugin install fast-jev-compaction@fast-jev-compaction --config mode=$COMPACTION_MODE`.

To change mode later, uninstall and re-run with the other `COMPACTION_MODE`.

## Uninstall

```bash
claude plugin uninstall fast-jev-compaction@fast-jev-compaction
claude plugin marketplace remove fast-jev-compaction
```

Then remove `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS` from `settings.json` if
nothing else in that account needs it.
