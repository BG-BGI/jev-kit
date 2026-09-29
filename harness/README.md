# harness

**Experimental.** A loop that drives a repository's gate sweep to green with
Jev deciding every step and Claude only writing the edits it is handed. It is
the browser agent's split, "Jev decides, Claude writes", applied to a coding
loop whose steps fit a menu.

```bash
python3 -m harness.loop --repo <dir> [--max-rounds 6] [--budget-usd 2] [--json out.json]
```

The repository has to follow the `check-agent` contract: `make check-agent`
runs every gate with no early exit and writes `.agent/report.json` as
`{ok, failed[], gates[{name, ok, output}]}`.

## Each round

1. Run the sweep. All green ends the loop.
2. One Jev request, with three questions per failed gate
   ([harness/decide.py](decide.py)):
   - `fix_kind`: `autofix`, `code_edit`, `environment` or `needs_person`
   - `model`: the cheapest model enough to write the fix, `haiku` or `sonnet`
   - `same`: from round two, is this the failure the last attempt meant to fix?
3. Act in code, cheapest first ([harness/loop.py](loop.py)):
   - a confident `environment` or `needs_person` stops the loop and says why;
   - `autofix` gates run the fixers (`ruff check --fix`, `ruff format`), no LLM;
   - a gate Jev calls the same failure twice running is stuck, and the loop
     stops instead of paying for a third attempt;
   - everything else is one `claude -p` call to write the edits, on Haiku
     unless some gate needs Sonnet, with no Bash tool. The harness, not
     Claude, runs the gates.

## The A/B

```bash
python3 -m harness.bench --source <repo> --seed seed.json --trials 2 \
    --baseline-model sonnet --out results.md
```

Every trial starts from a fresh `git archive HEAD` copy of `--source`, with
`uv sync --all-groups` and the seed's edits applied. The source repository is
never written. The baseline is one `claude -p` told to run `make check-agent`,
fix every failed gate and re-run until green. Both arms run the CLI with the
same isolation flags: no user hooks or plugins, no MCP servers, no saved
session, a spend cap ([harness/claude_cli.py](claude_cli.py)). Both are judged
by one final sweep the bench runs itself.

A seed is JSON, `[{"file", "find", "replace"}]`. Each `find` must appear
exactly once, so a stale seed fails loudly instead of benchmarking a clean
tree. Seeds name real code, so none ships here.

Results: [docs/measurements.md](../docs/measurements.md#the-jev-decided-harness).
