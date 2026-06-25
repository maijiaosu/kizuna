#!/usr/bin/env python3
"""
Stop Hook — 智能质量门 + Plan 交付子 agent 审计。

只有会话中有文件修改（Write/Edit）时才拦。
纯对话（问问题、搜索）直接放行。
有修改 → 搜索 transcript 中的验证信号 → 有→放行, 无→block。
如果有 plan 文件 → 注入交付对照子 agent 审计块。

-- PLAN MATCHING --
Plan 文件名基于 session slug（如 "crystalline-zooming-iverson"）。
从 transcript 中提取 slug → 匹配 ~/.claude/plans/<slug>.md。
"""
import json, sys, os, re
from pathlib import Path

PLANS_DIR = Path.home() / ".claude" / "plans"

# 会改文件的操作
MUTATING_TOOLS = {"Write", "Edit", "NotebookEdit"}

# 验证类命令的匹配模式
VERIFY_COMMANDS = [
    "test", "pytest", "npm test", "npm run test", "yarn test", "pnpm test",
    "make test", "make check", "go test", "cargo test", "cargo check",
    "npx jest", "npx vitest", "python -m pytest", "python3 -m pytest",
    "mypy", "pyright", "ruff", "flake8", "black --check",
    "eslint", "prettier --check", "tsc --noEmit",
    "lint", "typecheck", "type-check", "benchmark",
    "诊断", "先测", "跑一下", "先跑",
]

# 用户认可消息的匹配关键词
USER_APPROVAL = ["OK", "ok", "好", "行", "可以", "没问题", "没问题了",
                 "VERIFICATION COMPLETE", "verified",
                 "不错", "干得不错", "尤里卡"]


def _count_tool_uses(lines: list) -> tuple[int, set]:
    """统计 transcript 中的工具调用。返回 (修改次数, 被修改文件集合)"""
    edit_count = 0
    files_edited = set()
    for line in lines:
        try:
            d = json.loads(line.strip())
            content = d.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") != "tool_use":
                    continue
                name = c.get("name", "")
                inp = c.get("input", {})
                if name in MUTATING_TOOLS:
                    edit_count += 1
                    fp = inp.get("file_path", "")
                    if fp:
                        files_edited.add(os.path.basename(fp))
        except Exception:
            continue
    return edit_count, files_edited


def _find_verification_signals(lines: list) -> tuple[bool, str]:
    """在 transcript 中搜索验证信号。返回 (找到?, 描述)"""
    signals = []
    for line in lines:
        try:
            d = json.loads(line.strip())
        except Exception:
            continue

        # 检查 assistant 消息中的 tool_use
        content = d.get("message", {}).get("content", [])
        if isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use" and c.get("name") == "Bash":
                    cmd = c.get("input", {}).get("command", "")
                    desc = c.get("input", {}).get("description", "")
                    for sig in VERIFY_COMMANDS:
                        if sig in cmd or sig in desc:
                            signals.append(f"测试/lint 命令: {cmd[:80]}")

        # 检查用户消息内容
        if d.get("type") == "user":
            msg = d.get("message", {})
            user_text = msg.get("content", "")
            if isinstance(user_text, str):
                for ok_word in USER_APPROVAL:
                    if ok_word in user_text:
                        signals.append(f"用户认可: '{user_text[:60]}'")
                        break

        # 检查 tool_result 中的 exit code
        if d.get("type") == "user" and "toolUseResult" in d:
            result = d.get("toolUseResult", {})
            if isinstance(result, dict):
                exit_code = result.get("exitCode", -1)
                stderr = result.get("stderr", "")
                if exit_code == 0 and not stderr:
                    pass  # 太弱，不计入

    found = len(signals) > 0
    return found, "; ".join(signals[:3]) if signals else ""


# ─── Plan delivery audit ───────────────────────────────────────────────


def _extract_slug(lines: list) -> str:
    """从 transcript 中提取 session slug。"""
    for line in lines:
        try:
            d = json.loads(line.strip())
            slug = d.get("slug", "")
            if slug:
                return slug
        except Exception:
            continue
    return ""


def _find_plan_file(slug: str) -> Path | None:
    """匹配 plan 文件。先精确匹配，再模糊。"""
    if not slug:
        return None
    exact = PLANS_DIR / f"{slug}.md"
    if exact.exists():
        return exact
    # 模糊匹配
    try:
        for f in PLANS_DIR.glob("*.md"):
            if slug[:20] in f.name or f.name.replace(".md", "") in slug:
                return f
    except Exception:
        pass
    return None


def _extract_modified_files(lines: list) -> list[str]:
    """从 transcript 中提取所有被修改的文件路径。"""
    files = set()
    for line in lines:
        try:
            d = json.loads(line.strip())
            content = d.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") != "tool_use":
                    continue
                name = c.get("name", "")
                inp = c.get("input", {})
                if name in MUTATING_TOOLS:
                    fp = inp.get("file_path", "")
                    if fp:
                        files.add(fp)
        except Exception:
            continue
    return sorted(files)


def _build_plan_audit_block(plan_path: Path, modified_files: list[str]) -> str:
    """构建 plan 交付对照子 agent 审计指令块。"""
    try:
        plan_content = plan_path.read_text()[:4000]
    except Exception:
        return ""

    files_text = "\n".join(f"- {f}" for f in modified_files[:30]) if modified_files else "(无文件修改)"

    return f"""
[Plan Delivery Sub-Agent Audit]
Plan 文件：{plan_path.name}
Plan 内容（前 4000 字符）：\n{plan_content}

实际修改的文件：\n{files_text}

在继续之前，你必须用 Agent 工具并行 spawn 3 个子 agent。每个只审一个维度，互不知对方存在：

**子 agent 5a（逐条对照）：**
逐条提取 plan 中的交付项，对照实际修改的文件列表。每条标注：✅ 已交付 / ⚠️ 部分交付 / ❌ 缺失。不要漏掉 plan 中任何一条承诺。
→ 输出格式：逐条 checklist，每条标注状态 + 一句话证据（从修改文件列表中找）

**子 agent 5b（验证存在性）：**
标注为 ✅ 或 ⚠️ 的项，实际有验证行为吗？（测试/lint/运行/端到端检查）。claim 完成 ≠ 真的完成。
→ 输出格式：逐条标注 has_verification: yes/no + 一句话理由

**子 agent 5c（多余物）：**
有没有交付了但 plan 没写的东西？这些可能是好事（顺手修了别的问题），也可能是坏事（注意力涣散偏离计划）。
→ 输出格式：列出多余物 + 判断 good/bad/neutral

返回后：任一 ❌ 或 no_verification → 先补齐，再结束。全通过 → 正常结束。"""


def main():
    data = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read().strip()
            if raw:
                data = json.loads(raw)
        except json.JSONDecodeError:
            pass

    # Guard: 二次触发直接放行
    if data.get("stop_hook_active", False):
        print(json.dumps({"decision": "approve"}))
        return

    transcript_path = data.get("transcript_path", "")
    if not transcript_path or not Path(transcript_path).exists():
        # 无法读取 transcript → 不拦截（避免误伤）
        print(json.dumps({"decision": "approve"}))
        return

    # 读 transcript
    try:
        raw_lines = Path(transcript_path).read_text().splitlines()
        lines = [l for l in raw_lines if l.strip()]
    except Exception:
        print(json.dumps({"decision": "approve"}))
        return

    # 检测是否有修改行为
    edit_count, files = _count_tool_uses(lines)

    if edit_count == 0:
        # 纯对话 / 搜索 / 查询 → 不拦
        print(json.dumps({"decision": "approve"}))
        return

    # 有修改 → 检查是否有 plan 文件
    slug = _extract_slug(lines)
    plan_path = _find_plan_file(slug) if slug else None
    modified_files = _extract_modified_files(lines) if plan_path else []

    # 搜索验证信号
    verified, signals = _find_verification_signals(lines)

    # 构建 plan 审计块（如果有 plan）
    plan_audit_block = ""
    if plan_path and modified_files:
        plan_audit_block = _build_plan_audit_block(plan_path, modified_files)

    if verified and not plan_audit_block:
        print(json.dumps({"decision": "approve"}))
        return

    # 构建输出
    if not verified:
        # 无验证信号 → 拦截
        file_list = "、".join(sorted(files)[:8]) if files else "未知文件"
        if len(files) > 8:
            file_list += f" 等 {len(files)} 个文件"

        verify_prompt = f"""\
[STOP GATE]
本次会话修改了 {edit_count} 次文件: {file_list}
未检测到任何验证行为（测试/lint/benchmark/用户确认）。

请在结束前做至少一项验证，然后回复结果。"""

        output = {
            "decision": "block",
            "reason": f"{edit_count} 次文件修改无验证",
            "systemMessage": verify_prompt,
        }
    else:
        output = {"decision": "approve"}

    # 无论 block 还是 approve，有 plan 就注入审计
    if plan_audit_block:
        output["hookSpecificOutput"] = {
            "hookEventName": "Stop",
            "additionalContext": plan_audit_block,
        }

    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
