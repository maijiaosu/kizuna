"""
Search Audit plugin — post-WebSearch/WebFetch quality checks.

Inherited from hand-rolled Claude Code harness.

Uses ``transform_tool_result`` to append audit warnings inline with search
results so the model sees them directly. ``post_tool_call`` is NOT used
because Hermes discards its return value — only ``transform_tool_result``
can modify what the model sees.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ── Narrow query patterns ─────────────────────────────────────────────
# These queries should use direct web_extract, not broad web_search

NARROW_QUERY_PATTERNS = [
    r"(?:install|安装|下载).*(?:size|大小|多大|MB|GB|体积|空间)",
    r"(?:version|版本|latest|最新|更新)",
    r"(?:setup\.py|pyproject\.toml|package\.json|Cargo\.toml)",
    r"(?:README|readme|文档|doc|changelog)",
    r"(?:API|api).*(?:key|token|endpoint|secret)",
    r"(?:价格|pricing|多少钱|收费|免费)",
    r"^(?:什么是|what is|who is|where is).{0,20}$",
]

# ── Suspicious SEO / content-farm domains ─────────────────────────────

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

# ── LRU dedup cache (bounded) ─────────────────────────────────────────

_AUDIT_CACHE_MAX = 500
_audited: OrderedDict[str, int] = OrderedDict()
_audited_lock = threading.Lock()


def _mark_audited(tool_call_id: str) -> bool:
    """Returns True if this tool_call_id was already audited."""
    if not tool_call_id:
        return False
    with _audited_lock:
        if tool_call_id in _audited:
            _audited.move_to_end(tool_call_id)
            return True
        _audited[tool_call_id] = 1
        while len(_audited) > _AUDIT_CACHE_MAX:
            _audited.popitem(last=False)
        return False


# ── Audit logic ───────────────────────────────────────────────────────


def _audit_search(tool_name: str, tool_input: Dict, tool_response: Dict) -> Dict:
    """Run all regex-based audit checks. Returns {passed, issues, warnings, suggestions}."""
    issues: List[str] = []
    warnings: List[str] = []
    suggestions: List[str] = []

    query = tool_input.get("query", "") or tool_input.get("url", "")
    if not query:
        return {"passed": True, "issues": [], "warnings": [], "suggestions": []}

    # 1. Method audit: narrow questions shouldn't use broad search
    if tool_name in ("web_search", "WebSearch"):
        for pattern in NARROW_QUERY_PATTERNS:
            if re.search(pattern, query, re.IGNORECASE):
                issues.append(
                    f"方法错误：查询「{query[:80]}」是窄问题，应直接用 web_extract "
                    f"读源头（GitHub/setup.py/官方文档），不该用 web_search 搜"
                )
                suggestions.append("改用 web_extract 直接读源头")
                break

    # 2. Source quality audit
    results = tool_response.get("results", [])
    if isinstance(results, list):
        for r in results:
            url = ""
            if isinstance(r, dict):
                url = r.get("url", "")
            elif isinstance(r, str):
                url = r
            for domain in SUSPICIOUS_DOMAINS:
                if domain in url:
                    warnings.append(
                        f"[⚠ 可疑来源] {domain} — 可能是 SEO 聚合/内容农场，"
                        f"信息经二次转录，优先用它验证一手来源"
                    )

    # 3. Coverage diversity
    if isinstance(results, list) and len(results) >= 5:
        domains = set()
        for r in results:
            url = ""
            if isinstance(r, dict):
                url = r.get("url", "")
            elif isinstance(r, str):
                url = r
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

    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "suggestions": suggestions,
    }


def _format_audit_block(audit: Dict) -> str:
    """Render audit results into a Markdown block."""
    parts = []
    if audit.get("issues"):
        parts.append("## 🔍 搜索审计 — 未通过")
        for iss in audit["issues"]:
            parts.append(f"- ✗ {iss}")
    elif audit.get("warnings"):
        parts.append("## 🔍 搜索审计 — 通过（有警示）")

    if audit.get("warnings"):
        for w in audit["warnings"]:
            parts.append(f"- {w}")

    if audit.get("suggestions"):
        parts.append("\n**建议:**")
        for s in audit["suggestions"]:
            parts.append(f"- → {s}")

    if audit.get("warnings") and not audit.get("issues"):
        parts.append("\n请在输出中标明来源可信度。")

    return "\n".join(parts)


# ── Sub-agent audit block ─────────────────────────────────────────────
# Based on original harness sub-agent audit (dimensions 3+4).
# In the original Claude Code harness this was injected via
# hookSpecificOutput.additionalContext.  In Hermes the equivalent is
# appending it to the tool result so the model sees it inline and
# can call delegate_task to run the audits.


def _build_sub_agent_block(
    query: str,
    tool_response: Dict,
    max_results: int = 10,
) -> str:
    """Build a sub-agent audit instruction block for delegate_task.

    Tells the model to spawn 4 parallel sub-agents, each auditing one
    dimension of search quality independently.
    """
    results_summary = json.dumps(
        tool_response.get("results", [])[:max_results],
        ensure_ascii=False,
        default=str,
    )[:3000]

    return f"""
[Search Sub-Agent Audit — 维度 3 & 4]

搜索词：{query[:300]}
搜索结果：{results_summary}

在上述搜索结果返回后、产出任何基于这些结果的内容之前，你必须用 delegate_task 工具并行 spawn 4 个子 agent。每个只审一个维度，互不知对方存在：

**子 agent 1（覆盖面）：**
搜索结果之间的实质差异够不够？有没有漏掉某类来源（闭源商业产品、学术论文、专利、非英文资料）？我们是不是只看了某个单一生态而忽略了其他？
→ 输出格式：pass/warn + 一句话理由 + 如果 warn，建议补搜什么

**子 agent 2（精确度 / Cling）：**
搜索词是否精确 cling 到用户的原始问题？还是发散到了周边概念？逐字对照原始问题和搜索词。
→ 输出格式：pass/warn + 一句话理由

**子 agent 3（最优方法）：**
用户此时此景的提问，搜索是不是最优响应？还是应该直接去源头读，或者根本不需要搜？
→ 输出格式：pass/warn + 一句话理由

**子 agent 4（幻觉流）：**
搜索链条中是否在某一步被污染的结果引导到了完全错误的方向？特别注意：搜索结果是否在回答一个用户没问的问题。
→ 输出格式：pass/warn + 一句话理由

返回后：任一 warn → 先修正（换词/换方法/补来源），再回应用户。全 pass → 正常输出。"""


# ── Hook ──────────────────────────────────────────────────────────────


def _on_transform_tool_result(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    tool_call_id: str = "",
    **kwargs: Any,
) -> Optional[str]:
    """Append audit warnings and sub-agent audit block to tool result.

    This is the ONLY hook that can modify what the model sees —
    post_tool_call return values are discarded by Hermes.
    """
    if tool_name not in ("web_search", "web_extract", "WebSearch", "WebFetch"):
        return None

    if not isinstance(args, dict) or not isinstance(result, str):
        return None

    # Deduplicate
    if _mark_audited(tool_call_id):
        return None

    try:
        tool_response = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None

    audit = _audit_search(tool_name, args, tool_response)

    # Always append regex audit if there's anything to report
    blocks: list[str] = []
    if not audit["passed"] or audit["warnings"]:
        blocks.append(_format_audit_block(audit))

    # Append sub-agent audit block (dimensions 3+4) when results exist
    query = (args.get("query") or args.get("url") or "")
    if query and tool_response.get("results"):
        sub_block = _build_sub_agent_block(query, tool_response)
        if sub_block.strip():
            blocks.append(sub_block.strip())

    if not blocks:
        return None

    # Inject audit into JSON structure so Hermes renders it.
    # Appending text after the closing brace gets swallowed by the JSON parser.
    audit_text = "\n\n---\n".join(blocks)
    if isinstance(tool_response, dict):
        tool_response["_audit"] = audit_text
        return json.dumps(tool_response, ensure_ascii=False)
    # Fallback for non-dict JSON (shouldn't happen but be safe)
    return result + "\n\n---\n" + audit_text


def register(ctx) -> None:
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
