#!/usr/bin/env python3
"""
Minimal tests for stop_hook.py — verifies quality gate logic.

Run: python3 -m pytest tests/test_stop_hook.py -v
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# Allow importing from scripts/
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

# We don't directly import stop_hook (it uses stdin/stdout),
# so we test the helper functions by extracting them.

import importlib.util


def _load_mod(name: str) -> "module":
    """Load a script as a module without executing main()."""
    path = Path(__file__).parent.parent / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stop_hook_imports():
    """Smoke test: all scripts are importable."""
    for name in ["stop_hook", "db", "pre_tool_use_hook", "pre_compact_hook",
                 "user_prompt_hook", "session_review"]:
        mod = _load_mod(name)
        assert mod is not None, f"Failed to import {name}"


def test_mutating_tools_detection():
    """Verify stop_hook correctly identifies mutating tools."""
    transcript = [
        json.dumps({
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Write",
                     "input": {"file_path": "/tmp/test.py"}}
                ]
            }
        }),
        json.dumps({
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "ls -la"}}
                ]
            }
        }),
    ]
    lines = transcript
    mod = _load_mod("stop_hook")
    count, files = mod._count_tool_uses(lines)
    assert count == 1  # Write counts, Bash(ls) doesn't
    assert "test.py" in files


def test_verify_signals_detection():
    """Verify stop_hook detects test/lint commands in transcript."""
    transcript = [
        json.dumps({
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "python3 -m pytest tests/", "description": "run tests"}}
                ]
            }
        }),
    ]
    mod = _load_mod("stop_hook")
    found, desc = mod._find_verification_signals(transcript)
    assert found is True
    assert "pytest" in desc


def test_no_modifications_should_not_block():
    """Pure conversation (no file writes) should return approve."""
    transcript = [json.dumps({"message": {"content": [{"type": "text", "text": "hello"}]}})]
    mod = _load_mod("stop_hook")
    count, _ = mod._count_tool_uses(transcript)
    assert count == 0


def test_db_connection():
    """Smoke test: harness.db can be opened and queried."""
    mod = _load_mod("db")
    db_mod = mod.get_db()
    assert db_mod is not None
    stats = db_mod.stats()
    assert "total_sessions" in stats


def test_guardrail_compilation():
    """Verify guardrail block is compiled from DB anti_patterns."""
    mod = _load_mod("session_review")
    block = mod._get_guardrail_block()
    assert isinstance(block, str)
    # Should at minimum contain the default fallback if DB is empty
    assert len(block) > 0
