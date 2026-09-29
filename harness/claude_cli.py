"""One `claude -p` run, with its cost read from the CLI's own JSON result.

Both arms of the bench go through here with the same isolation flags, so the
only thing that differs between them is who decides what to do next:

  --setting-sources project,local   no user hooks, plugins or output styles
  --strict-mcp-config               no MCP servers
  --no-session-persistence          nothing left behind in ~/.claude/projects
  --max-budget-usd                  a hard spend ceiling per run

Cost is `total_cost_usd` as the CLI reports it, never tokens multiplied by a
price.
"""
import json
import subprocess
import time

BASE_FLAGS = (
    "--output-format", "json",
    "--setting-sources", "project,local",
    "--strict-mcp-config",
    "--no-session-persistence",
    "--permission-mode", "acceptEdits",
)
TIMEOUT_S = 1800


def run(prompt, cwd, model, allowed_tools, max_budget_usd, disallowed_tools=()):
    """{"ok", "cost_usd", "seconds", "turns", "result", "model_usage", "error"}."""
    cmd = ["claude", "-p", prompt, "--model", model, "--max-budget-usd", str(max_budget_usd),
           "--allowedTools", *allowed_tools]
    if disallowed_tools:
        cmd += ["--disallowedTools", *disallowed_tools]
    cmd += list(BASE_FLAGS)
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                              timeout=TIMEOUT_S, stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "cost_usd": None, "seconds": TIMEOUT_S, "error": "timeout"}
    seconds = round(time.monotonic() - started, 1)
    try:
        data = json.loads(proc.stdout)
    except Exception:
        return {"ok": False, "cost_usd": None, "seconds": seconds,
                "error": (proc.stderr or proc.stdout)[-500:]}
    return {
        "ok": not data.get("is_error"),
        "cost_usd": data.get("total_cost_usd"),
        "seconds": seconds,
        "turns": data.get("num_turns"),
        "result": str(data.get("result") or "")[-800:],
        "model_usage": {k: {"cost": (v or {}).get("costUSD")} for k, v in
                        (data.get("modelUsage") or {}).items()},
        "error": None if not data.get("is_error") else str(data.get("subtype")),
    }
