#!/usr/bin/env python3
"""
Session Review Hook — SQLite-backed.
SessionEnd: write session record to harness.db.
SessionStart: query DB for unreviewed sessions + pending skills, inject context.
PostToolUse: verify background Bash tasks.

Usage:
    python3 session_review.py session_end   (reads stdin JSON)
    python3 session_review.py session_start (reads stdin JSON)
    python3 session_review.py post_tool_use (reads stdin JSON)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DIR = Path.home() / ".claude" / "memory"
PENDING_DIR = MEMORY_DIR / "_pending_review"

MEMORY_CHAR_LIMIT = 3000
MEMORY_ENTRY_CHAR_LIMIT = 600
MAX_ENTRIES_PER_FILE = 8

# ─── USER.md profile compression ───────────────────────────────────────

def _compress_section(text: str, max_chars: int) -> str:
    """Compress a USER.md bullet-list section to max_chars by extracting key phrases."""
    bullets = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- "):
            period_pos = stripped.find("。")
            if 0 < period_pos < 60:
                bullets.append(stripped[2:period_pos])
            else:
                bullets.append(stripped[2:80])
    result = ""
    for b in bullets:
        candidate = result + ("；" if result else "") + b
        if len(candidate) > max_chars:
            break
        result = candidate
    return result

def _get_user_profile() -> str:
    """从 DB 读取 preference + knowledge，压缩到 ≤220 字注入。"""
    try:
        from db import get_db
        db = get_db()
        prefs = db.get_by_category("preference", limit=5)
        knowledge = db.get_by_category("knowledge", limit=3)
    except Exception:
        return ""

    parts = []
    # 按 utility 排序，只注入最高效的条目，硬上限 220 字
    prefs_sorted = sorted(prefs, key=lambda p: p.get("utility", 0), reverse=True)
    for p in prefs_sorted:
        title = p.get("title", "")
        content = p.get("content", "")
        first_line = ""
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("- ") and len(stripped) > 4:
                clean = stripped[2:].replace("**", "")
                period = clean.find("。")
                first_line = clean[:period] if 0 < period < 80 else clean[:80]
                break
        if not first_line:
            continue
        label = title[:4] if len(title) <= 4 else title[:6]
        candidate = f"{label}: {first_line}"
        # 检查是否超限
        test = " | ".join(parts + [candidate] + ([f"知识: ..."] if knowledge else []))
        if len(test) > 220:
            break
        parts.append(candidate)
    # 知识基线
    if knowledge:
        for k in knowledge:
            k_lines = [l.strip("- ") for l in k["content"].split("\n") if l.strip().startswith("-")]
            for line in k_lines:
                if any(w in line for w in ["已掌握", "数学分析", "正在学习", "抽象代数"]):
                    cand = f"知识: {line[:80]}"
                    if len(" | ".join(parts + [cand])) <= 220:
                        parts.append(cand)
                    break
            break

    result = " | ".join(parts)
    if len(result) > 220:
        result = result[:217] + "..."
    return result

# ─── Guardrail block (persistent session-level guards) ──────────────────

def _default_guardrail_block() -> str:
    """Fallback guardrail when DB is empty. Returns empty — users grow their own."""
    return ""

def _get_guardrail_block() -> str:
    """从 anti_pattern 的 Prevent 字段自动编译 guardrail block。≤800 字。"""
    try:
        from db import get_db
        db = get_db()
        aps = db.get_by_category("anti_pattern", limit=12)
    except Exception:
        return _default_guardrail_block()

    if not aps:
        return _default_guardrail_block()

    lines = []
    for ap in aps:
        prevent_text = ap.get("content", "")
        for line in prevent_text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("**Prevent:**"):
                prevent = stripped.replace("**Prevent:**", "").strip()
                if prevent:
                    lines.append(f"- {prevent}")
                break

    if not lines:
        return ""

    result = "[Active Guardrails]\n" + "\n".join(lines)
    if len(result) > 800:
        result = result[:797] + "..."
    return result

def _get_effective_block() -> str:
    """从 effective_pattern 的 When 字段编译可复用模式 block。≤400 字。"""
    try:
        from db import get_db
        db = get_db()
        effs = db.get_by_category("effective_pattern", limit=8)
    except Exception:
        return ""

    if not effs:
        return ""

    lines = []
    for eff in effs:
        content = eff.get("content", "")
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("**When:**"):
                when = stripped.replace("**When:**", "").strip()
                if when:
                    lines.append(f"- {when}")
                break

    if not lines:
        return ""

    result = "[Effective Patterns — reuse when applicable]\n" + "\n".join(lines)
    if len(result) > 400:
        result = result[:397] + "..."
    return result

# ─── Ensure directories ─────────────────────────────────────────────────
PENDING_DIR.mkdir(parents=True, exist_ok=True)


def cmd_session_end(data: dict):
    """SessionEnd: write session record to DB + run maintenance."""
    try:
        from db import get_db
        db = get_db()
        db.run_maintenance()
    except Exception:
        return

    session_id = data.get("session_id", "unknown")
    if session_id == "unknown":
        return

    # 统计工具调用次数（用于 skill distillation）
    transcript_path = data.get("transcript_path", "")
    tool_count = 0
    if transcript_path:
        try:
            tc = Path(transcript_path).read_text()
            tool_count = tc.count('"type":"tool_use"')
        except Exception:
            tool_count = 0

    db.upsert_session(
        session_id=session_id,
        cwd=data.get("cwd", os.getcwd()),
        tool_count=tool_count,
        skill_worthy=1 if tool_count >= 8 else 0,
        reviewed=0,
    )


def cmd_session_start(data: dict):
    """SessionStart: 从 DB 读 unreviewed sessions + pending skills，注入 context。"""
    try:
        from db import get_db
        db = get_db()
        db.run_maintenance()
        sessions = db.get_unreviewed_sessions(limit=15)
        skill_worthy = db.get_skill_worthy_sessions(limit=5)
        pending_skills = db.get_pending_skills()
    except Exception:
        return

    user_nudge = ""

    # Auto-review sessions with 0 tool calls — nothing to learn
    zero_tool = [s for s in sessions if s.get("tool_count", 0) == 0]
    sessions = [s for s in sessions if s.get("tool_count", 0) > 0]
    if zero_tool:
        try:
            _db2 = get_db()
            for s in zero_tool:
                _db2.upsert_session(session_id=s["id"], reviewed=1)
        except Exception as e:
            print(f"[session_review] auto-review failed: {e}", file=sys.stderr)

    if not sessions and not pending_skills:
        return

    count = len(sessions)
    # Build session summary
    summaries = []
    for s in sessions[-10:]:
        ts = (s.get("timestamp", "?")[:16] or "?").replace("T", " ")
        summaries.append(
            f"- {ts} | session: {s.get('id','?')[:8]}... "
            f"| cwd: {Path(s.get('cwd','?')).name} "
            f"| tools: {s.get('tool_count',0)}"
        )
    summary_text = "\n".join(summaries)

    # USER profile + guardrail block + effective patterns
    user_profile = _get_user_profile()
    guardrail_block = _get_guardrail_block()
    effective_block = _get_effective_block()

    profile_block = f"[User Profile] {user_profile}\n\n" if user_profile else ""
    guard_block = f"{guardrail_block}\n\n" if guardrail_block else ""
    eff_block = f"{effective_block}\n\n" if effective_block else ""

    # Skill extraction nudge
    skill_extraction_nudge = ""
    if skill_worthy:
        sessions_str = ", ".join(
            f"{s['id'][:8]}...({s['tool_count']} tools)"
            for s in skill_worthy
        )
        skill_extraction_nudge = f"""
[Skill Distillation Opportunity]
{len(skill_worthy)} recent session(s) meet complexity threshold: {sessions_str}

If you spot a reusable multi-step pattern:
→ Use Bash to INSERT INTO skill_proposals (name, content, description)
→ Never write directly to skills/ — requires approval via DB resolve_skill.
"""

    # Pending skills nudge
    skill_nudge = ""
    if pending_skills:
        skill_list = []
        for ps in pending_skills[:5]:
            name = ps.get("name", "?")
            desc = ps.get("description", "(no description)")
            skill_list.append(f"- **{name}**: {desc}")
        skill_nudge = f"""
[Pending Skill Proposals — Approval Required]
{len(pending_skills)} draft(s) await review:
{chr(10).join(skill_list)}

To approve: "approve skill <name>" → resolve_skill(name, True)
To reject: "reject skill <name>" → resolve_skill(name, False)
Never activate without explicit user approval.
"""

    nudge_extra = ""
    if skill_extraction_nudge:
        nudge_extra += skill_extraction_nudge
    if skill_nudge:
        nudge_extra += skill_nudge

    review_instructions = profile_block + guard_block + eff_block + f"""
[Session Review]
{count} unreviewed session(s):

{summary_text}

For each session that contains a valuable lesson:

1. Anti-pattern: Use {Path.home() / '.claude/scripts/db.py'} → db.upsert_memory(category='anti_pattern', ...)
   Format (EXACT markers required — guardrail compiler only reads **Prevent:**):
   **Prevent:** <one-sentence rule for future sessions>
   **Re:** <what actually went wrong>
   **Root:** <why it happened>

2. Effective pattern: db.upsert_memory(category='effective_pattern', ...)
   Format:
   **When:** <situation where this pattern applies>
   **What:** <what worked>
3. If NOTHING worth saving: db.upsert_session(id=..., reviewed=1)

Anti-bloat rules:
- anti_pattern: max 12 entries. effective_pattern/guardrail/knowledge/preference: max 8.
- Prefer UPDATE over INSERT (use upsert_memory with same title).
- Only store CROSS-PROJECT patterns.
- anti_pattern 满 12 条时：列出全部已有条目，问我要删哪条还是覆盖。禁止自行决定替换。

Mark session as reviewed when done: db.upsert_session(id=..., reviewed=1)
{user_nudge}
{nudge_extra}
"""

    msg_parts = []
    if count > 0:
        msg_parts.append(f"{count} session(s) pending review")
    system_msg = "Session review: " + (", ".join(msg_parts) if msg_parts else "nothing urgent.")
    if user_nudge:
        system_msg += " (USER.md may need a look)"

    has_context = count > 0 or bool(pending_skills) or bool(user_nudge)
    output = {
        "systemMessage": system_msg,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": review_instructions if has_context else "",
        },
    }
    print(json.dumps(output, ensure_ascii=False))


# ─── Maintenance ────────────────────────────────────────────────────────

def cleanup_stale():
    """Remove pending items older than PENDING_TTL_DAYS."""
    cutoff = datetime.now(timezone.utc).timestamp() - (PENDING_TTL_DAYS * 86400)
    for f in PENDING_DIR.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except Exception:
            pass


def enforce_pending_cap():
    """Ensure pending items don't exceed MAX_PENDING_ITEMS."""
    files = sorted(PENDING_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime)
    while len(files) > MAX_PENDING_ITEMS:
        try:
            files[0].unlink()
        except Exception:
            pass
        files = files[1:]


# ─── PostToolUse verification ─────────────────────────────────────────────
VERIFICATION_FAILURES_DIR = MEMORY_DIR / "_verification_failures"

# Minimum expected sizes for common file types (bytes)
SIZE_THRESHOLDS = {
    ".pt": 100 * 1024 * 1024,     # PyTorch model ≥ 100MB
    ".npz": 100 * 1024 * 1024,    # NumPy archive ≥ 100MB
    ".bin": 50 * 1024 * 1024,     # Model binary ≥ 50MB
    ".safetensors": 100 * 1024 * 1024,
}
DOWNLOAD_PATTERNS = [
    r"-o\s+(\S+)",           # curl -o /path
    r"--output\s+(\S+)",     # curl --output /path
    r">\s*(\S+)",            # shell redirect
    r"cp\s+\S+\s+(\S+)",     # cp src dst
    r"mv\s+\S+\s+(\S+)",     # mv src dst
]


def _extract_output_path(command: str) -> str | None:
    """Try to extract the output file path from a shell command."""
    for pattern in DOWNLOAD_PATTERNS:
        m = re.search(pattern, command)
        if m:
            path = m.group(1).strip('\'"')  # strip shell quoting
            # Expand ~ and resolve
            path = os.path.expanduser(path)
            return path
    return None


def _check_output_validity(path: str) -> dict:
    """Check if an output file looks valid for its type."""
    result = {"exists": False, "size": 0, "suspicious": False, "reason": ""}
    if not os.path.exists(path):
        result["reason"] = f"文件不存在: {path}"
        result["suspicious"] = True
        return result

    result["exists"] = True
    result["size"] = os.path.getsize(path)

    # Check against size thresholds
    ext = os.path.splitext(path)[1].lower()
    threshold = SIZE_THRESHOLDS.get(ext)
    if threshold and result["size"] < threshold:
        result["suspicious"] = True
        result["reason"] = (
            f"文件似乎不完整: {path} ({result['size'] / 1024 / 1024:.0f}MB)"
            f" — 通常应 ≥ {threshold / 1024 / 1024:.0f}MB"
        )

    # Check if it's an HTML error page (LFS redirect failure)
    if result["size"] < 10000:
        try:
            with open(path, "rb") as f:
                head = f.read(200)
            if head.startswith(b"<!DOCTYPE") or head.startswith(b"<html"):
                result["suspicious"] = True
                result["reason"] = f"文件是 HTML 错误页，不是真实数据: {path}"
        except Exception:
            pass

    return result


# ─── Search Audit ─────────────────────────────────────────────────────────

# 窄问题模式：这些查询应直接用 WebFetch 读源头，不该用 WebSearch
NARROW_QUERY_PATTERNS = [
    r"(?:install|安装|下载).*(?:size|大小|多大|MB|GB|体积|空间)",
    r"(?:version|版本|latest|最新|更新)",
    r"(?:setup\.py|pyproject\.toml|package\.json|Cargo\.toml)",
    r"(?:README|readme|文档|doc|changelog)",
    r"(?:API|api).*(?:key|token|endpoint|secret)",
    r"(?:价格|pricing|多少钱|收费|免费)",
    r"^(?:什么是|what is|who is|where is).{0,20}$",
]

# SEO 聚合/内容农场域名 — 不拦截但打标签
SUSPICIOUS_DOMAINS = [
    "php.cn",
    "developer.baidu.com",
    "zol.com.cn",
    "weiyangx.com",
    "playground.ru",
    "egltours.com",
    "ithome.com",
    "baijing.cn",
    "news.17173.com",
    "enduins.com",
    "4gamers.com.tw",
]


def _audit_search(tool_name: str, tool_input: dict, tool_response: dict) -> dict:
    """WebSearch/WebFetch 行为自审计。返回 {passed, issues, warnings, suggestions}。"""
    issues = []
    warnings = []
    suggestions = []

    query = tool_input.get("query", "") or tool_input.get("url", "")
    if not query:
        return {"passed": True, "issues": [], "warnings": [], "suggestions": []}

    # ── 1. 方法审计：窄问题不该用搜索 ──
    if tool_name == "WebSearch":
        for pattern in NARROW_QUERY_PATTERNS:
            if re.search(pattern, query, re.IGNORECASE):
                issues.append(
                    f"方法错误：查询「{query[:80]}」是窄问题，应直接用 WebFetch 读源头"
                    f"（GitHub/setup.py/官方文档），不该用 WebSearch 搜"
                )
                suggestions.append("改用 WebFetch 直接读源头")
                break

    # ── 2. 来源质量审计 ──
    results = tool_response.get("results", [])
    if isinstance(results, list):
        for r in results:
            if isinstance(r, dict):
                url = r.get("url", "")
                for domain in SUSPICIOUS_DOMAINS:
                    if domain in url:
                        warnings.append(
                            f"[⚠ 可疑来源] {domain} — 可能是 SEO 聚合/内容农场，"
                            f"信息经二次转录，优先用它验证一手来源"
                        )
            elif isinstance(r, str):
                for domain in SUSPICIOUS_DOMAINS:
                    if domain in r:
                        warnings.append(
                            f"[⚠ 可疑来源] {domain} — 可能二次转录，需验证"
                        )

    # ── 3. 覆盖面再审视 ──
    if isinstance(results, list) and len(results) >= 5:
        domains = set()
        for r in results:
            url = ""
            if isinstance(r, dict):
                url = r.get("url", "")
            elif isinstance(r, str):
                url = r
            from urllib.parse import urlparse
            try:
                netloc = urlparse(url).netloc
                if netloc:
                    domains.add(netloc)
            except Exception:
                pass
        if len(domains) <= 2:
            issues.append(
                f"来源多样性不足：{len(results)} 个结果仅来自 {len(domains)} 个域名，"
                f"可能需要换搜索词/角度重新搜"
            )
            suggestions.append("换搜索角度重新搜以获得更广泛的来源")

    # ── 3-4 由子 agent 处理，不在此处做正则 ──

    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "suggestions": suggestions,
    }


# ─── Sub-agent audit injection ──────────────────────────────────────────

LAST_PROMPT_FILE = str(Path.home() / ".claude" / "memory" / "_last_user_prompt.json")


def _read_last_user_prompt() -> str:
    """读取 UserPromptSubmit hook 存的原始用户问题。"""
    try:
        with open(LAST_PROMPT_FILE) as f:
            data = json.loads(f.read())
        return data.get("prompt", "")
    except Exception:
        return ""


def _build_sub_agent_injection(query: str, tool_response: dict) -> str:
    """构建子 agent 审计指令块。只做 3 和 4（3+4a+4b+4c）。"""
    user_prompt = _read_last_user_prompt()
    if not user_prompt:
        return ""

    # 提取搜索结果摘要
    results_summary = json.dumps(
        tool_response.get("results", [])[:10],
        ensure_ascii=False, default=str
    )[:3000]

    return f"""
[Search Sub-Agent Audit — 维度 3 & 4]
用户原始问题：{user_prompt[:500]}
搜索词：{query[:300]}
搜索结果：{results_summary}

在上述工具返回结果后、产出任何内容之前，你必须用 Agent 工具并行 spawn 4 个子 agent。每个只审一个维度，互不知对方存在：

**子 agent 3（覆盖面）：**
搜索结果之间的实质差异性够不够？有没有漏掉某类来源（闭源商业产品、学术论文、专利、非英文资料）？我们是不是只看了某个单一生态而忽略了其他？
→ 输出格式：pass/warn + 一句话理由 + 如果 warn，建议补搜什么

**子 agent 4a（Cling）：**
搜索词是否精确 cling to 用户的原始问题？还是发散到了周边概念？逐字对照原始问题和搜索词。
→ 输出格式：pass/warn + 一句话理由

**子 agent 4b（最优方法）：**
用户此时此景的提问，搜索是不是最优响应？还是应该直接去源头读、或者根本不需要搜？
→ 输出格式：pass/warn + 一句话理由

**子 agent 4c（幻觉流）：**
搜索链条中是否在某一步被污染的结果引导到了完全错误的方向？特别注意：搜索结果是否在回答一个用户没问的问题。
→ 输出格式：pass/warn + 一句话理由

返回后：任一 warn → 先修正（换词/换方法/补来源），再回应用户。全 pass → 正常输出。"""

def cmd_post_tool_use(data: dict):
    """PostToolUse hook: search audit + background Bash task verification."""
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    tool_response = data.get("tool_response", {})

    # ── Search audit (WebSearch / WebFetch) ──
    if tool_name in ("WebSearch", "WebFetch"):
        audit = _audit_search(tool_name, tool_input, tool_response)

        # Build dimensions 1-2 audit message (regex)
        parts = []
        if audit["issues"]:
            parts.append("问题:")
            for iss in audit["issues"]:
                parts.append(f"  ✗ {iss}")
        if audit["warnings"]:
            parts.append("警示:")
            for w in audit["warnings"]:
                parts.append(f"  {w}")
        if audit["suggestions"]:
            parts.append("建议:")
            for s in audit["suggestions"]:
                parts.append(f"  → {s}")
        audit_text = "\n".join(parts)
        severity = "未通过" if not audit["passed"] else "通过（有警示）"
        has_issues = parts

        # Build sub-agent injection for dimensions 3-4
        query = tool_input.get("query", "") or tool_input.get("url", "")
        sub_agent_block = _build_sub_agent_injection(query, tool_response)

        if not audit["passed"]:
            # Regex failed → block
            print(json.dumps({
                "systemMessage": f"🔍 搜索审计{severity}",
                "continue": False,
                "stopReason": f"搜索审计{severity}：{'; '.join(audit['issues']) if audit['issues'] else ''}{'; '.join(audit['suggestions']) if audit['suggestions'] else ''}",
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"[Search Audit: {severity}]\n{audit_text}\n\n请重新搜索或改用其他方法。"
                    ),
                },
            }, ensure_ascii=False, default=str))
            return

        # Regex passed → split into two channels to avoid truncation:
        #   systemMessage: dimension 1-2 audit summary (visible warnings)
        #   additionalContext: dimension 3-4 sub-agent instructions only
        audit_summary = ""
        if has_issues:
            audit_summary = (
                f"[Search Audit: {severity}]\n{audit_text}\n\n"
                f"请在输出中标明来源可信度。"
            )

        if not audit_summary and not sub_agent_block:
            return  # 一切正常，无需注入

        sys_msg = f"🔍 搜索审计{severity}"
        if audit_summary:
            sys_msg += f"\n\n{audit_summary}"

        print(json.dumps({
            "systemMessage": sys_msg,
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": sub_agent_block or "",
            },
        }, ensure_ascii=False, default=str))
        return

    # ── Background Bash task verification ──
    if tool_name != "Bash":
        return

    # Only check background tasks
    if not tool_input.get("run_in_background"):
        return

    command = tool_input.get("command", "")
    description = tool_input.get("description", "")
    exit_code = tool_response.get("exitCode", -1)

    is_download = bool(
        re.search(r'https?://', command) or
        any(kw in description.lower() for kw in ["download", "下载", "curl", "wget",
                                                   "pip install", "pip3 install"])
    )

    if is_download:
        # Extract output path for download verification
        output_path = _extract_output_path(command)
        if not output_path:
            return  # Can't determine output path — nothing to verify
        validity = _check_output_validity(output_path)
    else:
        # Non-download background task: check exit code + stderr
        validity = {"exists": True, "size": 0, "suspicious": False, "reason": ""}
        stderr = tool_response.get("stderr", "")
        if isinstance(stderr, str) and stderr.strip():
            error_signals = [
                "error", "Error", "ERROR", "fatal", "Fatal", "Traceback",
                "traceback", "Permission denied", "command not found",
                "No such file", "cannot", "failed", "Failed"
            ]
            if any(s in stderr for s in error_signals):
                validity["suspicious"] = True
                validity["reason"] = f"stderr包含错误信号: {stderr[:150]}"

    if not validity["suspicious"]:
        return  # Looks fine

    # Write verification failure
    VERIFICATION_FAILURES_DIR.mkdir(parents=True, exist_ok=True)
    failure = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": data.get("session_id", "unknown"),
        "command": command[:300],
        "description": description[:200],
        "exit_code": exit_code,
        "output_path": output_path if is_download else "(non-download task)",
        "file_size": validity["size"],
        "reason": validity["reason"],
    }

    if is_download:
        safe_name = os.path.basename(output_path).replace("/", "_")[:80]
    else:
        safe_name = f"bg_task_{int(time.time())}"
    path = VERIFICATION_FAILURES_DIR / f"{safe_name}_{int(time.time())}.json"
    path.write_text(json.dumps(failure, indent=2, ensure_ascii=False))

    # Output systemMessage so user sees the warning
    reason_short = validity["reason"][:150]
    print(json.dumps({
        "systemMessage": f"⚠️ 怀疑任务异常: {reason_short}",
        "continue": True,
    }, ensure_ascii=False))


# ─── Entry point ────────────────────────────────────────────────────────
if __name__ == "__main__":
    data = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read().strip()
            if raw:
                data = json.loads(raw)
        except json.JSONDecodeError:
            pass

    cmd = sys.argv[1] if len(sys.argv) > 1 else "session_end"

    if cmd == "session_end":
        cmd_session_end(data)
    elif cmd == "session_start":
        cmd_session_start(data)
    elif cmd == "post_tool_use":
        cmd_post_tool_use(data)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
