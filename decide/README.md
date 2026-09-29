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

The `context` and every question and option, through `airlock/redact.py`,
with the context capped at 60,000 characters. Nothing Claude did not put in
the call. Each call is a local row in `~/.local/state/airlock/decide.jsonl`:
the decision names, the answers, latency and Jev cost.

## Limits

- Up to 20 decisions a call, 255 options per choice, 10 levels per score.
- Jev reads about 32k tokens of state. A context past that is cut, and a
  decision about the part that was cut is a guess.
- The tool is only as useful as Claude's willingness to call it. Nothing makes
  Claude reach for it; its description says when to. Whether it saves Claude
  spend in practice is not measured yet.
