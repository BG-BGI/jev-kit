# Skill suggest

A `UserPromptSubmit` hook that names at most one skill for the turn, so the
agent loads the right `SKILL.md` or none at all. A wrong load is expensive: thousands of
tokens that sit in context for every later turn of the session. TypeSafe's
[skill-suggestion cookbook](../docs/jev-reference/typesafe-docs/cookbooks_skill_suggestion.md)
cut that mistake by more than half on 488 requests against Haiku 4.5. Wrong
loads fell from 16.8% to 7.3%, and loads when nothing fits from 9.8% to 4.0%.

This is that recipe, over the roster your session was actually shown.

## What it does

1. **Code first, free.** A slash command, a `!` line, a turn of fewer than
   four words, or a turn nobody typed (a `<task-notification>`, a
   `<cross-session-message>`) is never sent anywhere.
2. **Roster.** The session's own transcript carries the skill list Claude Code
   showed the model (a `skill_listing` attachment). That list is the roster,
   because it holds skills no file describes, like `init` or org-served
   `anthropic-skills:pptx`. `SKILL.md` files under the project,
   `~/.claude/skills` and installed plugins supply the full descriptions.
   A session's first prompt arrives before that listing is written, so each
   listing seen is saved per directory in
   `~/.local/state/airlock/skill-listing.json`, and a first turn uses the last
   one saved for its directory.
3. **Request 1** ranks the whole roster with one `Choice`, and asks three
   `Noul`s whether the turn wants an action at all. Their mean under 0.20
   suggests nothing.
4. **Request 2** rereads the top three with each skill's full description and
   the opening of its `SKILL.md`, plus one "does this skill fit" `Noul` each.
   The suggested skill's own fit has to reach 0.50: the Choice's winner if
   it does, else the best-fitting of the three if that one does, else nothing.
5. **One line of context.** It uses the cookbook's measured wording:

```
<skill_relevance>
Relevant to the current request: ros2. Ignore this if it does not fit what the user actually asked for.
</skill_relevance>
```

When nothing fits it says `No skill in the roster appears relevant to this
request.` The system prompt's skill index is never touched, so prompt caching
over it still holds.

## Install

```bash
install/install.sh --skill-suggest --wire ~/.claude/settings.json
```

That deploys the guard's release, which carries the hook, and adds one
`UserPromptSubmit` entry with an 8 s timeout. Without `--wire` nothing is
edited. `install/wire.sh --print --skill-suggest <settings.json>` previews it.
It needs the same `TYPESAFE_API_KEY` as the guard, and with no key it does
nothing.

## Switches

| | |
|---|---|
| `echo shadow > ~/.config/airlock/skill-suggest` | judge and log, emit nothing |
| `echo off > ~/.config/airlock/skill-suggest` | do nothing |
| `AIRLOCK_SKILL_SUGGEST=on\|shadow\|off` | the same, for one shell |
| `AIRLOCK_DISABLE=1` or `~/.config/airlock/disabled` | the kit-wide kill switch wins |

Every judged turn is a row in `~/.local/state/airlock/skill-suggest.jsonl`:
the skill, why, the gate, the shortlist, each fit, latency and Jev tokens.

## What leaves the machine

The prompt, through `airlock/redact.py` and truncated. Every skill's name and
first 250 characters of description. For the three shortlisted skills, the
full description and the first 700 characters of `SKILL.md`. Nothing else: no
transcript text, no files, no tool results.

## Cost and latency

Two requests a turn, about 0.4 s end to end through the warm daemon on this
machine, and about 10,000 Jev input tokens against a 126-skill roster (output
tokens are free). A short or slash-command turn costs nothing.

## Measure it

```bash
python3 -m suggest.eval          # eval/skill-roster.json + eval/skill-cases.jsonl
```

38 synthetic cases against a 28-skill roster with close pairs on purpose
(new deck and edit deck, launch files and tf2). 28 cases have a right skill and
10 have none. It scores the two numbers the cookbook moved, `wrong_skill` and
`load_when_none`. Results: [docs/measurements.md](../docs/measurements.md#skill-suggest).
