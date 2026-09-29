"""Run a repository's agent gate sweep and read its report.

The contract is the one scout-v2's `make check-agent` follows: run every gate
with no early exit and write `.agent/report.json` as
{"ok": bool, "failed": [names], "gates": [{"name", "ok", "output"}]}.
Any repository that writes that file can be driven by this harness.
"""
import json
import subprocess
import time
from pathlib import Path

REPORT = Path(".agent") / "report.json"
OUTPUT_TAIL = 2500
GATE_TIMEOUT_S = 900


def run(repo, command=("make", "check-agent")):
    """Run the sweep in `repo`. Returns (report dict, seconds). A missing or
    unreadable report is {"ok": False, "failed": ["<no report>"], ...}."""
    started = time.monotonic()
    subprocess.run(list(command), cwd=str(repo), capture_output=True, text=True,
                   timeout=GATE_TIMEOUT_S, check=False)
    seconds = round(time.monotonic() - started, 1)
    try:
        report = json.loads((Path(repo) / REPORT).read_text())
    except Exception as exc:
        report = {"ok": False, "failed": ["<no report>"],
                  "gates": [{"name": "<no report>", "ok": False, "output": str(exc)}]}
    return report, seconds


def failed_gates(report):
    """[(name, output tail)] for every failed gate, in report order."""
    out = []
    for gate in report.get("gates") or []:
        if not gate.get("ok"):
            out.append((str(gate.get("name")), str(gate.get("output") or "")[-OUTPUT_TAIL:]))
    return out
