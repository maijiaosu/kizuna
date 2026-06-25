# Kizuna (絆) — AI Agent Quality Control System

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

## What Kizuna Does

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

## Hermes Agent

Kizuna's architecture has been ported to [Hermes Agent](https://github.com/NousResearch/hermes-agent)
as 4 independent plugins. Same design philosophy, different hook protocol.

| Hermes Plugin | Maps To | Hook |
|---|---|---|
| `stop-gate` | Quality gate + plan audit | `post_tool_call` / `pre_llm_call` |
| `search-audit` | Search method + source audit | `transform_tool_result` |
| `download-guard` | Post-download verification | `transform_tool_result` |
| `guard-compiler` | Anti-pattern → guardrail injection | `pre_llm_call` |

**Install:**
```bash
cp -r hermes-plugins/* ~/.hermes/plugins/
hermes plugins enable stop-gate search-audit download-guard guard-compiler
```

**Key difference:** The Hermes port uses `pre_llm_call` context injection instead
of Claude Code's native `decision: "block"`. Same control logic, softer enforcement
layer — the model *can* ignore the gate, but it's staring at a full-screen warning
every turn until it complies.

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

> 我曾思考，如果给一个智商 100 的人装上机械臂，他能否去做智商 150 的人才能理解的事情？
> 这放在人身上似乎是荒谬的，但放在智能体……我觉得未必。
>
> 通过这个作品，我希望每个人都能让廉价的模型发挥出顶模的性能——
> 但这还差得远，这也远不是终点。
>
> —— 麦角蘇
