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

## Tool Permissions

| Tool          | Permitted | Scope                                     |
|---------------|-----------|-------------------------------------------|
| Bash          | Yes       | Git, Python, curl, SQLite, pip install    |
| Write/Edit    | Yes       | Project files only; no system paths       |
| WebSearch     | Yes       | Research and fact-checking                |
| WebFetch      | Yes       | GitHub, PyPI, documentation sites         |
| Bash(rm -rf)  | Blocked   | PreToolUse hook intercepts destructive ops|
| Bash(git push)| Blocked   | Requires explicit user approval           |

## Architecture Boundaries

- **Writable:** `scripts/`, `~/.claude/scripts/`, `~/.claude/memory/`
- **Read-only:** system directories, other projects
- **Never touch:** `~/.claude/memory/harness.db` directly (use db.py API)

## MCP / External Tools

None required. All tools are built-in Claude Code tools. No external MCP servers needed.

## Inspectable State

- SQLite database at `~/.claude/memory/harness.db` — query with `python3 scripts/db.py`
- Session transcripts in `~/.claude/projects/`
- Hook logs in `~/.claude/logs/`
- PreCompact state snapshots in `~/.claude/memory/_compact_state.json`
