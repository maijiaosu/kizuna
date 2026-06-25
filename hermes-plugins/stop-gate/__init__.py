"""
Stop Gate plugin — quality gate for file modifications without verification.
Also: Plan delivery sub-agent audit when plan files exist.

Inherited from hand-rolled Claude Code harness.

Wires four behaviours:

1. post_tool_call — tracks file mutations and verification signals.
2. pre_llm_call — injects STOP GATE + optional Plan audit block.
3. on_session_end — logs session-end stats for observability.
4. Plan delivery audit — when ~/.hermes/plans/*.md exists + unverified
   edits, injects a 3 sub-agent audit block for plan-vs-delivery check.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── Mutating tools ────────────────────────────────────────────────────

MUTATING_TOOLS = {"write_file", "patch", "skill_manage"}

MUTATING_BASH_PATTERNS = [
    r"\brm\b\s",
    r"\bmv\b\s",
    r"\bcp\b\s",
    r"\bsed\s+-[^-\s]*i",
    r"\btee\b\s",
    r">+\s*[^\s|&/]",
    r"\bdd\b\s.*of=",
]

VERIFY_COMMANDS = [
    "npm run test", "npm test", "yarn test", "pnpm test",
    "make test", "make check",
    "go test", "cargo test", "cargo check",
    "npx jest", "npx vitest",
    "python -m pytest", "python3 -m pytest",
    "pytest", "mypy", "pyright", "ruff", "flake8",
    "black --check", "eslint", "prettier --check",
    "tsc --noEmit", "typecheck", "type-check",
    "lint", "benchmark",
    "诊断", "先测", "跑一下", "先跑",
]

# ── Per-session state ─────────────────────────────────────────────────

_STATE_FILE = Path(__file__).parent / "state.json"
_MAX_STATE_ENTRIES = 200
_STATE_TTL_SECONDS = 7 * 86400

_state: Dict[str, Dict] = {}
_lock = threading.Lock()
_loaded = False


def _prune_stale(state: Dict[str, Dict]) -> None:
    cutoff = time.time() - _STATE_TTL_SECONDS
    for sid in list(state.keys()):
        ts = state[sid].get("last_gate_fired_at", 0)
        if ts and ts < cutoff:
            del state[sid]


def _load_state() -> Dict[str, Dict]:
    try:
        if _STATE_FILE.exists():
            data = json.loads(_STATE_FILE.read_text())
            if isinstance(data, dict):
                for s in data.values():
                    if isinstance(s.get("files_edited"), list):
                        s["files_edited"] = set(s["files_edited"])
                _prune_stale(data)
                return data
    except Exception:
        pass
    return {}


def _save_state_locked() -> None:
    try:
        serializable = {}
        for sid, s in list(_state.items())[-_MAX_STATE_ENTRIES:]:
            entry = dict(s)
            if isinstance(entry.get("files_edited"), set):
                entry["files_edited"] = sorted(entry["files_edited"])
            serializable[sid] = entry
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(serializable, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _get(session_id: str) -> Dict:
    global _loaded
    with _lock:
        if not _loaded:
            _state.update(_load_state())
            _loaded = True
        if session_id not in _state:
            _state[session_id] = {
                "edit_count": 0,
                "verify_count": 0,
                "files_edited": set(),
                "verification_signals": [],
                "last_gate_fired_at": 0,
            }
        return _state[session_id]


# ── Detection helpers ─────────────────────────────────────────────────

def _detect_bash_mutation(command: str) -> bool:
    for pattern in MUTATING_BASH_PATTERNS:
        if re.search(pattern, command):
            return True
    return False


def _detect_verification(command: str) -> Optional[str]:
    for sig in VERIFY_COMMANDS:
        if sig in command:
            return sig
    return None


def _extract_file_path(args: Dict[str, Any]) -> Optional[str]:
    path = args.get("path") or args.get("file_path") or ""
    if isinstance(path, str) and path:
        return Path(path).name
    return None


# ── post_tool_call ────────────────────────────────────────────────────

def _on_post_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    session_id: str = "",
    task_id: str = "",
    **kwargs: Any,
) -> None:
    if not session_id:
        return
    if not isinstance(args, dict):
        args = {}

    with _lock:
        if not _loaded:
            _state.update(_load_state())
            _loaded = True
        if session_id not in _state:
            _state[session_id] = {
                "edit_count": 0, "verify_count": 0,
                "files_edited": set(), "verification_signals": [],
                "last_gate_fired_at": 0,
            }
        s = _state[session_id]

        if tool_name in MUTATING_TOOLS:
            s["edit_count"] += 1
            fname = _extract_file_path(args)
            if fname:
                s["files_edited"].add(fname)
            _save_state_locked()

        elif tool_name == "terminal":
            command = args.get("command", "")
            if isinstance(command, str) and _detect_bash_mutation(command):
                s["edit_count"] += 1
                _save_state_locked()

        if tool_name == "terminal":
            command = args.get("command", "")
            if isinstance(command, str):
                sig = _detect_verification(command)
                if sig:
                    s["verify_count"] += 1
                    s["verification_signals"].append(sig)
                    if s["verify_count"] >= s["edit_count"]:
                        s["edit_count"] = 0
                        s["verify_count"] = 0
                        s["files_edited"].clear()
                        s["verification_signals"].clear()
                    _save_state_locked()


# ── Plan delivery audit ───────────────────────────────────────────────

_PLANS_DIR = Path.home() / ".hermes" / "plans"


def _find_active_plan() -> Optional[Path]:
    if not _PLANS_DIR.exists():
        return None
    try:
        now = time.time()
        candidates = []
        for f in _PLANS_DIR.glob("*.md"):
            try:
                age_hours = (now - f.stat().st_mtime) / 3600
                if age_hours < 24:
                    candidates.append((age_hours, f))
            except OSError:
                pass
        if candidates:
            candidates.sort()
            return candidates[0][1]
    except OSError:
        pass
    return None


def _build_plan_audit_block(files_edited: set) -> str:
    plan_path = _find_active_plan()
    if not plan_path:
        return ""
    try:
        plan_content = plan_path.read_text()[:4000]
    except OSError:
        return ""
    files_list = sorted(files_edited)
    files_text = "\n".join(
        f"- {f}" for f in files_list[:30]
    ) if files_list else "(no file modifications)"

    return f"""\
[Plan Delivery Sub-Agent Audit]
Plan file: {plan_path.name}
Plan content (first 4000 chars):
{plan_content}

Modified files:
{files_text}

Before continuing, spawn 3 parallel sub-agents with delegate_task. Each audits one dimension:

Sub-agent 5a (item-by-item cross-check):
Extract every deliverable from the plan. Cross-check against modified files.
Tag each: ✅ delivered / ⚠ partially delivered / ❌ missing.
Output: checklist with status + one-sentence evidence per item.

Sub-agent 5b (verification existence):
For items tagged ✅ or ⚠, was there actual verification (test/lint/run/e2e)?
Claiming done != actually done.
Output: has_verification: yes/no + one-sentence reason per item.

Sub-agent 5c (extras):
Anything delivered that was NOT in the plan? Good (bonus fixes) or bad (scope drift)?
Output: list of extras + good/bad/neutral judgement per item.

After results: any ❌ or no_verification → fix first, then end. All pass → proceed. """


# ── pre_llm_call ──────────────────────────────────────────────────────

def _on_pre_llm_call(
    session_id: str = "",
    is_first_turn: bool = False,
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    if not session_id or is_first_turn:
        return None

    with _lock:
        if not _loaded:
            _state.update(_load_state())
            _loaded = True
        if session_id not in _state:
            return None
        state = dict(_state[session_id])
        edit_count = state.get("edit_count", 0)
        verify_count = state.get("verify_count", 0)
        files_edited = set(state.get("files_edited", set()))

    if edit_count == 0 or verify_count >= edit_count:
        return None

    now = time.time()
    if now - state.get("last_gate_fired_at", 0) < 3:
        return None

    with _lock:
        _state[session_id]["last_gate_fired_at"] = now
        _save_state_locked()

    files_list = sorted(files_edited)
    file_str = "、".join(files_list[:8]) if files_list else "未知文件"
    if len(files_list) > 8:
        file_str += f" 等 {len(files_list)} 个文件"

    context = f"""\
[STOP GATE — Quality Check Required]
上一轮你修改了 {edit_count} 次文件: {file_str}
未检测到任何验证行为（测试/lint/typecheck/benchmark/端到端确认）。

在继续下一步之前，你必须做至少一项验证：
1. 运行相关测试（pytest / npm test / cargo test / ...）
2. 跑到 lint / typecheck（ruff / mypy / eslint / tsc --noEmit / ...）
3. 端到端验证用户完整流程，附结果

验证通过后再继续。不要跳过这一步。"""

    plan_audit = _build_plan_audit_block(files_edited)
    if plan_audit:
        context += "\n\n" + plan_audit

    return {"context": context}


# ── on_session_end ────────────────────────────────────────────────────

def _on_session_end(
    session_id: str = "",
    **kwargs: Any,
) -> None:
    if session_id:
        with _lock:
            s = _state.get(session_id)
            if s and s.get("edit_count", 0) > 0:
                logger.info(
                    "stop-gate: session %s ended with %d unverified edits on %s",
                    session_id[:8], s["edit_count"],
                    sorted(s.get("files_edited", set())),
                )


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
