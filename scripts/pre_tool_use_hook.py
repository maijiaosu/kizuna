#!/usr/bin/env python3
"""PreToolUse 安全拦截。根据 _tool_blocklist.json 检查 Bash 命令。
exit 2 = 硬拦截，exit 1 = 软拦截。stderr 输出原因供模型查看。"""
import json, re, sys
from pathlib import Path

BLOCKLIST_PATH = Path.home() / ".claude" / "memory" / "_tool_blocklist.json"

def load_blocklist() -> list:
    try:
        if BLOCKLIST_PATH.exists():
            data = json.loads(BLOCKLIST_PATH.read_text())
            return data.get("blocks", [])
    except Exception:
        pass
    return []

def main():
    data = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read().strip()
            if raw:
                data = json.loads(raw)
        except json.JSONDecodeError:
            pass

    tool_name = data.get("tool_name", "")
    if tool_name != "Bash":
        sys.exit(0)

    tool_input = data.get("tool_input", {})
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else str(tool_input)
    if not command:
        sys.exit(0)

    for block in load_blocklist():
        if block.get("type") != "regex":
            continue
        pattern = block.get("pattern", "")
        if not pattern:
            continue
        try:
            if re.search(pattern, command):
                severity = block.get("severity", "high")
                reason = block.get("reason", f"匹配拦截规则: {block.get('id','?')}")
                exit_code = 2 if severity == "critical" else 1
                print(
                    f"[PreToolUse BLOCK] {severity.upper()}: {reason}\n"
                    f"  命令: {command[:200]}",
                    file=sys.stderr,
                )
                sys.exit(exit_code)
        except re.error:
            continue

    sys.exit(0)

if __name__ == "__main__":
    main()
