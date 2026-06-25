# AGENTS.md — Kizuna (絆) Agent Quality Control

## For Claude Code

This repo contains the hook scripts and database layer that enforce quality
gates during Claude Code sessions. The hooks are wired via
`.claude/settings.local.json` (template provided).

**Rules are dynamic, not static.** At SessionStart, `session_review.py`
auto-compiles active guardrails from the SQLite database. Do not duplicate
rules here — add them to the DB via:

```bash
python3 scripts/db.py
# → db.upsert_memory(category='anti_pattern', title='...', content='...')
```

## Hook Overview

| Hook             | Script                        | Purpose                              |
|------------------|-------------------------------|--------------------------------------|
| SessionStart     | session_review.py session_start | Inject guardrails + user profile    |
| PreToolUse       | pre_tool_use_hook.py          | Block dangerous Bash commands        |
| PostToolUse      | session_review.py post_tool_use | Verify downloads + audit searches   |
| Stop             | stop_hook.py                  | Quality gate: require verification   |
| SessionEnd       | session_review.py session_end | Persist session to DB               |
| PreCompact       | pre_compact_hook.py pre       | Save context before compaction       |
| PostCompact      | pre_compact_hook.py post      | Restore context after compaction     |

## Key Principles

1. **Hard gates > soft prompts.** Exit code 2 blocks. Context injection forces.
2. **DB-driven rules.** Anti-patterns live in SQLite, not markdown files.
3. **Utility decay.** Memories lose weight over time unless accessed.
4. **Verify before proceed.** Stop hook scans transcript for test/lint signals.

## Claude Tool Policy

All Claude Code tools are explicitly declared. The `.claude/settings.json`
in this repo defines which tools the agent may use and under what conditions.

| Tool          | Policy       | Irreversible-Action Rules                          |
|---------------|-------------|---------------------------------------------------|
| Bash          | Allowed     | Git, Python, curl, SQLite, pip install             |
| Write / Edit  | Allowed     | Project files only; never touch system paths       |
| WebSearch     | Allowed     | Research and fact-checking                         |
| WebFetch      | Allowed     | GitHub, PyPI, documentation sites only             |
| Bash(rm -rf)  | BLOCKED     | PreToolUse hook exit 2 — destructive shell denied  |
| Bash(git push)| BLOCKED     | Requires explicit user approval via guardrail      |

## Permission Boundaries & Write Scopes

- **Writable directories:** `scripts/`, `~/.claude/scripts/`, `~/.claude/memory/`
- **Read-only:** system directories, other projects, configuration files
- **Irreversible actions:** Any Bash command matching destructive patterns is blocked
  by `pre_tool_use_hook.py` before execution. The blocklist is maintained in
  `~/.claude/memory/_tool_blocklist.json`.
- **Sensitive directories:** auth, billing, infra paths are never writable.

## MCP Servers & External Tool Interfaces

**MCP servers:** None required. All tools are built-in Claude Code tools.
**External tool interfaces:** None. The harness operates entirely within
Claude Code's native toolset (Bash, Write, Edit, WebSearch, WebFetch).

## CI Backstop

GitHub Actions workflow at `.github/workflows/test.yml` runs pytest on every push.
Local equivalent: `python3 -m pytest tests/ -v`.

## Inspectable State & Trace Logs

- **Trace logs:** `~/.claude/logs/` — hook execution logs, constraint extraction logs
- **Session checkpoints:** `~/.claude/memory/_compact_state.json` — PreCompact snapshots
- **Session transcripts:** `~/.claude/projects/` — full JSONL transcripts
- **Memory database:** `~/.claude/memory/harness.db` — SQLite+FTS5, all anti-patterns and
  effective patterns. Query via `python3 scripts/db.py`.

## Loop Prevention

Stop hook guards against infinite recovery loops: `stop_hook_active` flag
detected at line 212 of `stop_hook.py`. Secondary approval required on every
block decision — the hook never loops silently.
