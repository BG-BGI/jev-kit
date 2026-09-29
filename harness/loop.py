"""Drive a repository's gate sweep to green, with Jev deciding every step.

    python3 -m harness.loop --repo DIR [--max-rounds 6] [--budget-usd 2] [--json out.json]

Each round:

  1. run the sweep (harness/gates.py). All green: done.
  2. one Jev request classifies every failed gate (harness/decide.py).
  3. act in code, cheapest first:
       - any gate Jev calls `environment` or `needs_person` past STOP_BAR: stop
         and say so. A person has to act; no model call will fix it.
       - any `autofix` gate with a fixer configured: run the fixers, no LLM,
         and go round again.
       - `code_edit` gates Jev says are the SAME failure as last round, twice
         running: stuck. Stop rather than pay for a third attempt.
       - the rest: one `claude -p` call to write the edits, on the cheapest
         model Jev judged enough (haiku unless any gate needs sonnet), with
         no Bash: the harness, not Claude, runs the gates.

Claude writes text. It never chooses the next action.
"""
import argparse
import json
import subprocess
import sys
import time

from . import claude_cli, decide, gates

STOP_BAR = 0.8
SAME_BAR = 0.8
STUCK_AFTER = 2
EDIT_TOOLS = ("Read", "Edit", "Write", "Grep", "Glob")
FIXERS = {
    "lint": [["uv", "run", "ruff", "check", "--fix", "."]],
    "format-check": [["uv", "run", "ruff", "format", "."]],
}
MODEL_RANK = {"haiku": 0, "sonnet": 1}

PATCH_PROMPT = """You are fixing failing quality gates in this repository. The harness
runs the gates itself: do not run commands, only read and edit files. Follow the
repository's CLAUDE.md. Make the smallest change that passes each gate; never
weaken a gate, silence a check or add an ignore comment.

{gates}

When the edits are made, reply with one line per gate saying what you changed."""


def _gate_block(failed, names):
    return "\n\n".join("## Gate `%s` failed:\n```\n%s\n```" % (n, out)
                       for n, out in failed if n in names)


def _run_fixers(repo, names):
    ran = []
    for name in names:
        for cmd in FIXERS.get(name, []):
            subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, check=False)
            ran.append(" ".join(cmd))
    return ran


def run(repo, ask, max_rounds=6, budget_usd=2.0, log=print):
    started = time.monotonic()
    result = {"rounds": [], "claude_cost_usd": 0.0, "jev_cost_usd": 0.0,
              "jev_tokens": 0, "outcome": "max_rounds"}
    previous, attempts = {}, {}
    for number in range(1, max_rounds + 1):
        report, gate_s = gates.run(repo)
        row = {"round": number, "gate_seconds": gate_s, "failed": list(report.get("failed") or [])}
        result["rounds"].append(row)
        if report.get("ok"):
            result["outcome"] = "green"
            break
        failed = gates.failed_gates(report)
        verdicts = decide.decide(failed, previous, ask)
        usage = verdicts.pop("_usage")
        result["jev_tokens"] += usage["input_tokens"]
        result["jev_cost_usd"] += usage["cost_usd"]
        row["verdicts"] = verdicts
        log("round %d: failed %s" % (number, ", ".join(
            "%s=%s/%s" % (n, v["fix_kind"], v["model"]) for n, v in verdicts.items())))

        blockers = [n for n, v in verdicts.items()
                    if v["fix_kind"] in ("environment", "needs_person")
                    and v["fix_confidence"] >= STOP_BAR]
        if blockers:
            row["action"] = "stop_for_person"
            result["outcome"] = "needs_person: " + ", ".join(blockers)
            break

        auto = [n for n, v in verdicts.items() if v["fix_kind"] == "autofix" and n in FIXERS]
        if auto:
            row["action"] = "autofix"
            row["ran"] = _run_fixers(repo, auto)
            previous = dict(failed)
            continue

        edits = []
        for name, v in verdicts.items():
            attempts[name] = attempts.get(name, 0) + 1 if v["same"] >= SAME_BAR else 1
            if attempts[name] <= STUCK_AFTER:
                edits.append(name)
        if not edits:
            row["action"] = "stuck"
            result["outcome"] = "stuck: " + ", ".join(verdicts)
            break

        model = max((verdicts[n]["model"] for n in edits), key=lambda m: MODEL_RANK.get(m, 1))
        remaining = budget_usd - result["claude_cost_usd"]
        if remaining <= 0.05:
            result["outcome"] = "budget"
            break
        claude = claude_cli.run(PATCH_PROMPT.format(gates=_gate_block(failed, edits)),
                                repo, model, EDIT_TOOLS, round(remaining, 2),
                                disallowed_tools=("Bash",))
        row.update({"action": "claude_edit", "model": model, "gates": edits,
                    "claude": claude})
        result["claude_cost_usd"] += claude.get("cost_usd") or 0.0
        log("round %d: %s edited %s for $%.4f" % (number, model, ", ".join(edits),
                                                  claude.get("cost_usd") or 0.0))
        previous = dict(failed)
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m harness.loop")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--max-rounds", type=int, default=6)
    parser.add_argument("--budget-usd", type=float, default=2.0)
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    from airlock import client
    result = run(args.repo, client.ask, args.max_rounds, args.budget_usd)
    print("outcome=%s claude=$%.4f jev=$%.5f (%d tokens) seconds=%s rounds=%d" % (
        result["outcome"], result["claude_cost_usd"], result["jev_cost_usd"],
        result["jev_tokens"], result["seconds"], len(result["rounds"])))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(result, f, indent=1, default=str)
    return 0 if result["outcome"] == "green" else 1


if __name__ == "__main__":
    sys.exit(main())
