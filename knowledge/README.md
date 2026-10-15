# knowledge

> **Experimental.** This component is being built and is off until you opt in
> with `--knowledge`. Its numbers are not measured yet. The design is in
> [experience-graph](../docs/experience-graph.md).

An MCP server that lets Claude write to the experience graph and read it
back. It is **not the decide server**. `decide` is stateless judgment and
never writes. This server is stateful: it stores things. Two servers, two
contracts. Decide judges, knowledge remembers.

## Tools

- `graph_log(bucket, text, entities?, rejected_alternatives?, scope?)`
  writes one node. It goes through the same ingest path as a hook's
  candidate, so Jev gates it first. The result says what happened:
  `stored`, `dropped`, `duplicate`, `refused` or `off`.
- `graph_search(query?, bucket?)` reads the graph. It is local, with no Jev
  call and no network. With a query it returns the nearest nodes. Without
  one, the top nodes for the current repo.
- `graph_constraint_drop(id)` removes one node by id.

```json
{"bucket": "decision",
 "text": "Chose sqlite for the store: no daemon to run.",
 "entities": ["knowledge/store.py"],
 "rejected_alternatives": ["postgres", "a JSONL file"]}
```

A `decision` with no `rejected_alternatives` is refused before it reaches
the gate. A `constraint` is stored with no Jev call, redacted of token-like strings only, so the server
sends it as an explicit write. Only log one when the human said it.

## When Claude calls it

The server sends `instructions` on `initialize`, and Claude Code puts them in
the system prompt while the server is connected. They say to log when Claude
abandons an approach or chooses between named alternatives. They say to log a
`doc` when an outside reference answered something. Never log secrets, and
log a `constraint` only when the human stated one. Nobody edits a
`CLAUDE.md`. The text is `INSTRUCTIONS` in `server.py`, and a test pins its
key clauses.

There is no transcript mining. What Claude does not report at the time is
not captured.

## Register it

```bash
claude mcp add --scope user jev-knowledge -- \
  "$(command -v python3)" ~/.local/share/airlock/current/knowledge/server.py
```

`install/install.sh --knowledge` does this for you, along with the hooks.
Undo it with `claude mcp remove --scope user jev-knowledge`. The switch
`~/.config/airlock/knowledge` set to `off` stops writes without removing
anything.

## What leaves the machine

Only the gate call sends anything: the candidate text, redacted by
`airlock/redact.py`, goes to Jev with its nearest stored nodes. `graph_search`
and `graph_constraint_drop` never leave the machine. Text is redacted before
it is stored too, and the database is a mode 600 file in the state
directory.

## Limits

- Text is capped at 1,000 characters, and each entity or alternative at 300.
- Search returns at most five nodes, with 200-character previews.
- The server cannot check that a `constraint` came from the human. The
  trigger rule asks, and `graph_constraint_drop` is the way back.
