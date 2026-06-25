#!/usr/bin/env python3
"""PreCompact / PostCompact 状态保存。
PreCompact (pre): 保存上下文状态到 _compact_state.json
PostCompact (post): 读取状态文件，注入压缩恢复 context (≤300 字)

用法: python3 pre_compact_hook.py pre|post"""
import json, sys
from pathlib import Path
from datetime import datetime, timezone

STATE_FILE = Path.home() / ".claude" / "memory" / "_compact_state.json"

def cmd_pre(data: dict):
    trigger = data.get("compact_trigger", "auto")
    cwd = data.get("cwd", "")
    session_id = data.get("session_id", "unknown")

    state = {
        "compacted_at": datetime.now(timezone.utc).isoformat(),
        "trigger": trigger,
        "session_id": session_id,
        "cwd_snapshot": cwd,
    }

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))
    print(json.dumps({"continue": True}))


def cmd_post(data: dict):
    if not STATE_FILE.exists():
        print(json.dumps({"continue": True}))
        return

    try:
        state = json.loads(STATE_FILE.read_text())
        STATE_FILE.unlink()
    except Exception:
        print(json.dumps({"continue": True}))
        return

    trigger = state.get("trigger", "?")
    ts = (state.get("compacted_at", "")[:16] or "?").replace("T", " ")
    cwd_name = state.get("cwd_snapshot", "?").rsplit("/", 1)[-1]

    injection = (
        f"[上下文压缩恢复 @ {ts}]\n"
        f"压缩前你在 {cwd_name} 中工作（触发：{trigger}）。\n"
        f"如果丢失了上下文：回顾最近修改的文件，重建当前任务。"
    )

    if len(injection) > 300:
        injection = injection[:297] + "..."

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostCompact",
            "additionalContext": injection,
        },
    }, ensure_ascii=False))


if __name__ == "__main__":
    data = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read().strip()
            if raw:
                data = json.loads(raw)
        except json.JSONDecodeError:
            pass

    cmd = sys.argv[1] if len(sys.argv) > 1 else "pre"
    if cmd == "post":
        cmd_post(data)
    else:
        cmd_pre(data)
