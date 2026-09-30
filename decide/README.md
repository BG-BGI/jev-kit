# decide

An MCP server with one tool, `decide`. Claude calls it when a decision is a
pick between options it can name. Which file, which approach, is this true of
this text, how bad is this. Jev answers with a probability per option. It
never writes text.

What Claude saves is the reasoning it would otherwise write, and the context
it would otherwise read to make many small calls itself. A batch of decisions
over one context costs about what one costs, because Jev reads the context
once and answers every question in parallel.

```json
{"context": "Bug report: after 2.3 the dashboard shows every robot offline ...",
 "decisions": [
   {"name": "area", "question": "Which component most likely broke?",
    "options": {"dashboard_frontend": "the web UI",
                "telemetry_ingest": "the service that receives heartbeats"}},
   {"name": "severity", "kind": "score", "question": "How severe is this?",
    "options": ["cosmetic", "degraded, workaround exists", "blocking"]},
   {"name": "regression", "kind": "yesno",
    "question": "Is this a regression from a recent release?"}]}
```

Measured on that call, 2026-09-29: `telemetry_ingest` at 0.93, severity 1.05
("degraded"), regression true at 0.96. It took 168 ms and cost $0.00002.

## Files: where it saves tokens

A decision over text already in Claude's context is cheaper for Claude to make
itself: calling a tool costs another turn, and each turn re-sends the context.
Measured 2026-09-30, Claude on Haiku answered a trivial pick directly without
calling `decide`, and that was the right call. `decide` pays when Claude never
has to read the data.

- `context_file`: decide over a file the server reads, not Claude.
- `items_file`: one question over every non-blank line, JSONL row or JSON
  array element (up to 2,000), in chunks of about 20,000 characters, four
  requests at a time. Claude gets back counts, `not_confident`, examples of
  the labels named in `show` (up to 50), and a `results_file` holding every
  label. A file with more items than were read says `truncated`.

```json
{"items_file": "logs/robot.log", "question": "What severity is this line?",
 "options": {"error": "a fault", "warning": "degraded, still running", "info": "normal"},
 "show": ["error"]}
```

On a synthetic 300-line robot log, 2026-09-30, this labelled 299 of 300
lines as a hand check did, all 15 errors included. The one difference was a
"scan rate dropped to 10 Hz (expected 10)" line, which Jev called info. It
took five requests, 515 ms and $0.0018. A path that looks like a secret
store, as R1 judges it, is refused.

The A/B ran the same day on a synthetic 2,000-line log with 21 errors and 220
warnings. The prompt asked Claude to classify every line and give the counts
and the error line numbers. Sonnet 5.5 through `claude -p`, two trials per
arm, Bash off in both. Every run got every count and error line exactly right.

| arm | Claude $ | Jev $ | total $, median | turns |
|---|---|---|---|---|
| Claude reads the file (Read, Grep) | 0.198, 0.216 | | **0.207** | 9, 13 |
| Claude hands the path to `decide` | 0.097, 0.037 | 0.012, 0.007 | **0.077** | 4, 4 |

That is 2.7x cheaper at the median. n=2 on one synthetic file, so read it as
the direction, not the size.

Each answer carries `confident`, which is the kit's usual bar: confidence of
at least 0.8 and a margin of at least 0.4 over the runner-up. Below it, the
answer is a hint, not a decision.

## Register it

```bash
claude mcp add --scope user jev-decide -- \
  "$(command -v python3)" ~/.local/share/airlock/current/decide/server.py
```

That points at the deployed release, like the guard's hooks, and the same
`TYPESAFE_API_KEY` is used. With no key, every call returns a tool error
saying so.

## What leaves the machine

The `context`, any `context_file` or `items_file` content, and every question
and option, through `airlock/redact.py`. The context is capped at 60,000
characters and each item at 500. Nothing Claude did not put in
the call. Each call is a local row in `~/.local/state/airlock/decide.jsonl`:
the decision names, the answers, latency and Jev cost.

## Limits

- Up to 20 decisions a call, 255 options per choice, 10 levels per score.
- Jev reads about 32k tokens of state. A context past that is cut, and a
  decision about the part that was cut is a guess.
- The tool is only as useful as Claude's willingness to call it. Nothing makes
  Claude reach for it; its description says when to. Whether it saves Claude
  spend in practice is not measured yet.
