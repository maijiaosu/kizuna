"""
Guard Compiler plugin — auto-compile anti-patterns into an active guardrail
block injected every turn via pre_llm_call.

Inherited from hand-rolled Claude Code harness (_guards.json.legacy +
anti_patterns from harness.db).

Reads anti-pattern definitions from ``anti_patterns.json`` in this plugin
directory.  Each entry is a lesson-learned with a ``prevent`` rule.

Format of anti_patterns.json::

    [
      {
        "id": "guess-before-investigate",
        "title": "性能问题/报错→先诊断",
        "severity": "high",
        "prevent": "性能问题/报错/异常 → 先跑最小诊断拿数据，再讨论方案。没有数字不动代码。"
      },
      ...
    ]

The plugin compiles all ``prevent`` rules into a guardrail block (≤800 chars)
and injects it via ``pre_llm_call`` context so the model sees it before
every turn.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_GUARDS_FILE = Path(__file__).parent / "anti_patterns.json"
_MAX_CHARS = 800


def _load_anti_patterns() -> List[Dict[str, str]]:
    """Load anti-pattern definitions from anti_patterns.json."""
    try:
        if _GUARDS_FILE.exists():
            data = json.loads(_GUARDS_FILE.read_text())
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("guard-compiler: failed to load %s: %s", _GUARDS_FILE, exc)
    return []


def _compile_guardrails(anti_patterns: List[Dict[str, str]]) -> str:
    """Compile anti-patterns into a guardrail block, ≤800 chars."""
    if not anti_patterns:
        return ""

    lines: list[str] = ["[Active Guardrails — automatically compiled from lessons learned]"]

    for ap in anti_patterns:
        prevent = ap.get("prevent", "")
        severity = ap.get("severity", "high")
        if not prevent:
            continue
        emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}.get(severity, "")
        candidate = f"{emoji} {prevent}" if emoji else f"- {prevent}"

        # Check if adding this line would exceed budget
        test = "\n".join(lines + [candidate])
        if len(test) > _MAX_CHARS:
            remaining = len(anti_patterns) - anti_patterns.index(ap) - 1
            if remaining > 0:
                lines.append(f"... ({remaining} more, see recentSessionStart for full list)")
            break
        lines.append(candidate)

    if len(lines) <= 1:
        return ""

    return "\n".join(lines)


# Cache: reload guards only when file changes
_cached_mtime: float = 0
_cached_block: str = ""


def _get_guardrail_block() -> str:
    """Return compiled guardrail block, cached with mtime check."""
    global _cached_mtime, _cached_block
    try:
        mtime = _GUARDS_FILE.stat().st_mtime if _GUARDS_FILE.exists() else 0
    except OSError:
        mtime = 0

    if mtime != _cached_mtime or _cached_mtime == 0:
        patterns = _load_anti_patterns()
        _cached_block = _compile_guardrails(patterns)
        _cached_mtime = mtime
        if _cached_block:
            logger.debug(
                "guard-compiler: compiled %d guardrails (%d chars)",
                len(patterns), len(_cached_block),
            )

    return _cached_block


# ── Hook ──────────────────────────────────────────────────────────────


def _on_pre_llm_call(
    is_first_turn: bool = False,
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    """Inject compiled guardrails into every turn."""
    if is_first_turn:
        return None  # session start already has its own guard injection

    block = _get_guardrail_block()
    if not block:
        return None

    return {"context": block}


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
