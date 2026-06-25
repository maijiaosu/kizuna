# LLM Agent Harness — Production-Grade Quality Control System

A self-improving harness for Claude Code that enforces quality at every lifecycle point:
**quality gates, search audit, download verification, and auto-compiled guardrails**.

> Originally built for personal use. Now shared as a reference architecture for
> harness engineering best practices.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Claude Code Agent                  │
│                                                     │
│  SessionStart ──→ review + inject guardrails        │
│  PreToolUse   ──→ block dangerous commands          │
│  PostToolUse  ──→ verify downloads + audit searches │
│  Stop         ──→ quality gate + plan audit         │
│  SessionEnd   ──→ persist to SQLite                 │
└─────────────────────────────────────────────────────┘
```

## What This Harness Does

| Component | Hook | What It Prevents |
|---|---|---|
| **Stop Gate** | `Stop` | File modifications without verification (tests/lint/typecheck) |
| **Plan Auditor** | `Stop` | Plan-delivery gaps via 3 parallel sub-agent audits |
| **Search Audit** | `PostToolUse` | Wrong search strategy + SEO farm results + low coverage |
| **Download Guard** | `PostToolUse` | Incomplete downloads disguised as success |
| **Guard Compiler** | `SessionStart` | Repeat mistakes via auto-compiled anti-pattern DB |
| **Security Gate** | `PreToolUse` | Destructive shell commands via regex blocklist |
| **Memory DB** | All | SQLite+FTS5 with utility decay for persistent learning |

## Quick Start

```bash
# 1. Copy hook config
cp .claude/settings.template.json ~/.claude/settings.local.json
# Edit: set your ANTHROPIC_AUTH_TOKEN

# 2. Copy scripts
cp scripts/*.py ~/.claude/scripts/

# 3. Initialize DB
python3 scripts/db.py  # auto-creates harness.db on first call

# 4. Verify
python3 tests/test_stop_hook.py
```

## Requirements

- Python 3.11+
- Claude Code (any version with hooks support)
- SQLite3 (built-in)

## Design Philosophy

> **Hard guarantees, not prompt suggestions.**
>
> Every check in this harness runs as deterministic shell code outside the LLM's
> context window. The model cannot skip or talk its way around a hook — exit
> code 2 blocks actions, and injected context forces verification before
> proceeding.

## License

MIT

---

*"硬保证，不是软提示。" — Kuwabara Mai*

*每一条规则都来自真实的生产事故。*
*这个仓库不会替你写代码，但会确保你不把同一个坑踩两次。*
