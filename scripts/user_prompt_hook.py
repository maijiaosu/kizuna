#!/usr/bin/env python3
"""UserPromptSubmit hook: extract explicit constraints + save last user prompt.

Constraints: precise, mechanical (file scope, no explanation, format, etc.).
Guards/anti-patterns: handled semantically by persistent guardrail block.

Also saves the raw user message to a temp file so PostToolUse search audit
can access the original question for sub-agent behavioral audit.
"""
import json, sys, os

# ConstraintChecker is an optional dependency from feynman-tutor skill.
# Falls back gracefully if not installed.
try:
    sys.path.insert(0, os.path.expanduser("~/.claude/skills/feynman-tutor/scripts"))
    from constraint_checker import ConstraintChecker
    _has_constraint_checker = True
except ImportError:
    ConstraintChecker = None  # type: ignore
    _has_constraint_checker = False

# ─── Save last user prompt for search audit ───────────────────────────────

LAST_PROMPT_FILE = os.path.expanduser("~/.claude/memory/_last_user_prompt.json")

# ─── Message extraction ──────────────────────────────────────────────────

cc = ConstraintChecker() if ConstraintChecker is not None else None
data = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}

msg = ""
for key in ("user_message", "prompt", "text", "message"):
    if isinstance(data.get(key), str) and data[key].strip():
        msg = data[key].strip()
        break
if not msg:
    evt = data.get("event", {}) if isinstance(data.get("event"), dict) else {}
    for key in ("user_message", "prompt", "text"):
        if isinstance(evt.get(key), str) and evt[key].strip():
            msg = evt[key].strip()
            break
if not msg:
    if isinstance(data, str) and data.strip():
        msg = data.strip()

# ─── Persist last user prompt for search audit sub-agents ─────────────

if msg:
    try:
        from datetime import timezone
        last_prompt = {
            "session_id": data.get("session_id", ""),
            "prompt": msg,
            "timestamp": __import__('datetime').datetime.now(timezone.utc).isoformat(),
        }
        os.makedirs(os.path.dirname(LAST_PROMPT_FILE), exist_ok=True)
        with open(LAST_PROMPT_FILE, "w") as f:
            json.dump(last_prompt, f, ensure_ascii=False)
    except Exception:
        pass

# ─── Constraint extraction + de minimis filter ───────────────────────────

constraints = []
if cc is not None:
    try:
        raw = cc.extract(msg)
    except Exception:
        raw = []
    constraints = [
        c for c in raw
        if not (
            (c.get("type") == "required" and len(c.get("value", "").strip()) < 8) or
            (c.get("type") == "exclusion" and len(c.get("phrase", "").strip()) < 3)
        )
    ]

# ─── Debug log ───────────────────────────────────────────────────────────

LOG_DIR = os.path.expanduser("~/.claude/logs")
os.makedirs(LOG_DIR, exist_ok=True)
try:
    log_entry = json.dumps({
        "keys_found": [k for k in data if isinstance(data.get(k), str)],
        "msg_extracted": msg[:100] if msg else "(none)",
        "constraints_count": len(constraints),
    }, ensure_ascii=False)
    with open(os.path.join(LOG_DIR, "user_prompt_hook.log"), "a") as lf:
        lf.write(log_entry + "\n")
except Exception:
    pass

# ─── Early exit: nothing to inject ───────────────────────────────────────

if not constraints:
    print(json.dumps({"continue": True}))
    sys.exit(0)

# ─── Build injection block ───────────────────────────────────────────────

lines = ["[Loop-1 Constraint Injection]"]
for c in constraints:
    ct = c.get("type", "")
    sev = c.get("severity", "medium")
    emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}.get(sev, "⚪")
    if ct == "file_exclusion":
        files = ", ".join(c.get("files", []))
        lines.append(f"{emoji} DO NOT touch or mention: {files}")
    elif ct == "file_scope":
        files = ", ".join(c.get("files", []))
        lines.append(f"{emoji} ONLY modify these files: {files}")
    elif ct == "no_explanation":
        lines.append(f"{emoji} NO explanations. Direct answer/code only.")
    elif ct == "format":
        lines.append(f"{emoji} Output format: {c.get('format', '')}")
    elif ct == "must_reference":
        lines.append(f"{emoji} Must reference: {c.get('url', '')}")
    elif ct == "required":
        lines.append(f"{emoji} REQUIRED: {c.get('value', '')}")

lines.append("Self-check before responding: am I violating any of the above? If yes, fix before replying.")

# Full audit block only for ≥2 constraints or any critical/high severity
has_serious = any(c.get("severity") in ("critical", "high") for c in constraints)
if len(constraints) >= 2 or has_serious:
    lines.append("")
    lines.append("── STRUCTURED SELF-AUDIT ──")
    lines.append("BEFORE visible response: draft → check each constraint → revise → output final only.")
    lines.append("Do NOT output draft/audit process. Output clean final version only.")
    lines.append("── END AUDIT ──")
else:
    lines.append("(Check response against constraint above before outputting)")

# ─── Output ──────────────────────────────────────────────────────────────

print(json.dumps({
    "continue": True,
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "\n".join(lines),
    }
}, ensure_ascii=False))
