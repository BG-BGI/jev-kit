"""A/B: the Jev-decided loop against Claude driving the same loop itself.

    python3 -m harness.bench --source REPO --seed seed.json --trials 2 \\
        [--baseline-model sonnet] [--budget-usd 2] [--out results.md]

Every trial starts from a fresh copy: `git archive HEAD` of --source into a
temporary directory, `uv sync --all-groups`, then the seed's edits. The copy
is the only thing either arm touches; the source repository is never written.

  jev       harness/loop.py: Jev decides each step, fixers run in code, Claude
            only writes edits it is handed (haiku or sonnet, as Jev judges)
  baseline  one `claude -p` on --baseline-model told to run the sweep, fix
            every failed gate and re-run until green, with Bash for make/uv

Both arms use the same CLI isolation flags (harness/claude_cli.py). Success
is judged the same way for both: one final sweep run by the bench itself.

A seed file is JSON: [{"file": path, "find": text, "replace": text}], each
`find` required to appear exactly once, so a seed that no longer applies
fails loudly instead of benchmarking a clean tree.
"""
import argparse
import datetime
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

from . import claude_cli, gates, loop

BASELINE_PROMPT = """Run `make check-agent`. It runs every quality gate and writes
.agent/report.json as {ok, failed[], gates[]}. Read it, fix every failed gate by
changing the code (follow CLAUDE.md; never weaken a gate, silence a check or add
an ignore comment), and re-run `make check-agent` until ok is true. Then reply
with one line per gate saying what you changed."""
BASELINE_TOOLS = ("Read", "Edit", "Write", "Grep", "Glob",
                  "Bash(make:*)", "Bash(uv:*)", "Bash(git diff:*)", "Bash(cat:*)")


def prepare(source, seed, parent):
    work = Path(tempfile.mkdtemp(prefix="harness-", dir=parent))
    archive = subprocess.run(["git", "-C", str(source), "archive", "HEAD"],
                             capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(work)], input=archive.stdout, check=True)
    subprocess.run(["uv", "sync", "-q", "--all-groups"], cwd=str(work),
                   capture_output=True, check=True)
    for edit in seed:
        path = work / edit["file"]
        text = path.read_text()
        if text.count(edit["find"]) != 1:
            raise SystemExit("seed does not apply: %r in %s" % (edit["find"][:60], edit["file"]))
        path.write_text(text.replace(edit["find"], edit["replace"], 1))
    return work


def run_jev(work, budget_usd):
    from airlock import client
    r = loop.run(work, client.ask, budget_usd=budget_usd, log=lambda *_: None)
    return {"claude_cost_usd": r["claude_cost_usd"], "jev_cost_usd": r["jev_cost_usd"],
            "seconds": r["seconds"], "rounds": len(r["rounds"]), "outcome": r["outcome"],
            "detail": r}


def run_baseline(work, model, budget_usd):
    r = claude_cli.run(BASELINE_PROMPT, work, model, BASELINE_TOOLS, budget_usd)
    return {"claude_cost_usd": r.get("cost_usd") or 0.0, "jev_cost_usd": 0.0,
            "seconds": r.get("seconds"), "rounds": r.get("turns"),
            "outcome": "ran" if r.get("ok") else "error: %s" % r.get("error"), "detail": r}


def trial(arm, source, seed, parent, args):
    work = prepare(source, seed, parent)
    before, _ = gates.run(work)
    if arm == "jev":
        row = run_jev(work, args.budget_usd)
    else:
        row = run_baseline(work, args.baseline_model, args.budget_usd)
    after, _ = gates.run(work)
    row.update({"arm": arm, "seeded_failed": before.get("failed"),
                "green": bool(after.get("ok")), "final_failed": after.get("failed")})
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    else:
        row["work"] = str(work)
    return row


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def report(rows, args):
    lines = ["# Harness A/B: Jev-decided loop vs Claude-driven loop", "",
             "%s. Source `%s`, seed `%s`, %d trial(s) per arm, baseline model `%s`." % (
                 datetime.date.today().isoformat(), Path(args.source).name,
                 Path(args.seed).name, args.trials, args.baseline_model), "",
             "| arm | green | median Claude $ | median Jev $ | median total $ | median s |",
             "|---|---|---|---|---|---|"]
    for arm in ("jev", "baseline"):
        rs = [r for r in rows if r["arm"] == arm]
        if not rs:
            continue
        total = [(r["claude_cost_usd"] or 0) + (r["jev_cost_usd"] or 0) for r in rs]
        lines.append("| %s | %d/%d | %.4f | %.5f | %.4f | %.0f |" % (
            arm, sum(r["green"] for r in rs), len(rs),
            _median([r["claude_cost_usd"] for r in rs]),
            _median([r["jev_cost_usd"] for r in rs]), _median(total),
            _median([r["seconds"] for r in rs])))
    lines += ["", "Seeded failures: %s." % ", ".join(rows[0]["seeded_failed"] or []), ""]
    for r in rows:
        lines.append("- %s: green=%s, Claude $%.4f, Jev $%.5f, %ss, %s, final failed %s" % (
            r["arm"], r["green"], r["claude_cost_usd"] or 0, r["jev_cost_usd"] or 0,
            r["seconds"], r["outcome"], r["final_failed"]))
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m harness.bench")
    parser.add_argument("--source", required=True)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--arms", default="jev,baseline")
    parser.add_argument("--baseline-model", default="sonnet")
    parser.add_argument("--budget-usd", type=float, default=2.0)
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--out")
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    seed = json.loads(Path(args.seed).read_text())
    rows = []
    for _ in range(args.trials):
        for arm in args.arms.split(","):
            row = trial(arm, args.source, seed, args.workdir, args)
            print("%-8s green=%s claude=$%.4f jev=$%.5f %ss %s" % (
                arm, row["green"], row["claude_cost_usd"] or 0, row["jev_cost_usd"] or 0,
                row["seconds"], row["outcome"]), flush=True)
            rows.append(row)
    text = report(rows, args)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
