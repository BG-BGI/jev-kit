"""Progressive disclosure of per-session overhead.

Claude Code assembles every session's system prompt from the skill roster,
MCP server schemas and CLAUDE.md before any hook runs. A hook can add
context but never remove it, so this package cuts the fixed overhead at
the configuration layer instead: cold skills move out of auto-discovery
(skills.py), unused MCP servers get per-project disable lists (mcp.py),
and the global CLAUDE.md shrinks to a pointer whose full text a
SessionStart hook injects only where it applies (claudemd.py).

Every decision is derived from observed usage (the transcripts under
~/.claude/projects and the metrics DB), never from a hand-kept list, and
every apply has a restore. Like the rest of the kit, everything here
fails open: a missing transcript, an unreadable settings file or a parse
error means "change nothing", never a crash.
"""
