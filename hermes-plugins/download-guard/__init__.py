"""
Download Guard plugin — post-download file verification.

Inherited from hand-rolled Claude Code harness.

Uses ``transform_tool_result`` to append verification warnings inline with
terminal output so the model sees them. ``post_tool_call`` is NOT used
because Hermes discards its return value — only ``transform_tool_result``
can modify what the model sees.

Checks:
1. stderr error signals (404, 403, SSL, timeout, etc.)
2. Output file existence and size thresholds for model/data files
3. HTML error page detection (LFS redirect failures)
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── Size thresholds for common model / data file types ─────────────────

SIZE_THRESHOLDS: Dict[str, int] = {
    ".pt": 100 * 1024 * 1024,          # PyTorch model ≥ 100MB
    ".npz": 100 * 1024 * 1024,         # NumPy archive ≥ 100MB
    ".bin": 50 * 1024 * 1024,          # Model binary ≥ 50MB
    ".safetensors": 100 * 1024 * 1024, # SafeTensors ≥ 100MB
    ".gguf": 50 * 1024 * 1024,         # GGUF model ≥ 50MB
    ".pth": 50 * 1024 * 1024,          # PyTorch state dict ≥ 50MB
    ".onnx": 10 * 1024 * 1024,         # ONNX model ≥ 10MB
    ".whl": 1 * 1024 * 1024,           # Python wheel ≥ 1MB
    ".tar.gz": 1 * 1024 * 1024,        # Archive ≥ 1MB
    ".zip": 1 * 1024 * 1024,           # Archive ≥ 1MB
}

# ── Download command patterns ──────────────────────────────────────────
# Each tuple: (regex, has_capture_group)
# has_capture_group=True means the regex can extract an output path

DOWNLOAD_PATTERNS: list[tuple[str, bool]] = [
    (r"(?:curl|wget)\b.*\s(?:-o|--output)\s+['\"]?(\S+)['\"]?", True),
    (r"(?:curl|wget)\b.*\s-O\b", False),
    (r"(?:curl|wget)\b.*\s*>\s*['\"]?(\S+)['\"]?", True),
    (r"\bpip3?\s+install\b", False),
    (r"\bgit\s+clone\b", False),
    (r"(?:huggingface-cli|hf)\s+download", False),
]

# ── Error signals in stderr ────────────────────────────────────────────

ERROR_SIGNALS = [
    "error", "Error", "ERROR", "fatal", "Fatal", "Traceback",
    "traceback", "Permission denied", "command not found",
    "No such file", "cannot", "failed", "Failed",
    "404", "403", "401", "500", "503",
    "Connection refused", "Connection reset", "timed out",
    "certificate", "SSL", "TLS",
]

# ── Path extraction ────────────────────────────────────────────────────


def _extract_output_path(command: str, workdir: str = "") -> Optional[str]:
    """Try to extract the output file path from a shell command."""
    for pattern, has_group in DOWNLOAD_PATTERNS:
        if not has_group:
            continue
        try:
            m = re.search(pattern, command)
            if m and m.lastindex and m.lastindex >= 1:
                try:
                    path = m.group(1).strip("'\"")
                except IndexError:
                    continue
                path = os.path.expanduser(path)
                if workdir and not os.path.isabs(path):
                    path = os.path.join(workdir, path)
                return path
        except (re.error, IndexError):
            continue
    return None


def _is_download_command(command: str, description: str = "") -> bool:
    """Check if a command looks like a download operation."""
    if any(kw in description.lower() for kw in [
        "download", "下载", "curl", "wget", "pip install", "pip3 install",
        "git clone", "clone", "pull", "fetch",
    ]):
        return True
    for pattern, _ in DOWNLOAD_PATTERNS:
        try:
            if re.search(pattern, command):
                return True
        except re.error:
            continue
    return False


# ── File validity check ────────────────────────────────────────────────


def _check_output_validity(path: str) -> Dict:
    """Check if an output file looks valid for its type."""
    result: Dict = {
        "exists": False, "size": 0, "suspicious": False, "reason": "",
    }

    if not os.path.exists(path):
        result["reason"] = f"文件不存在: {path}"
        result["suspicious"] = True
        return result

    result["exists"] = True
    try:
        result["size"] = os.path.getsize(path)
    except OSError:
        result["reason"] = f"无法读取文件大小: {path}"
        result["suspicious"] = True
        return result

    # Check against size thresholds
    ext = os.path.splitext(path)[1].lower()
    if not ext and path.endswith(".tar.gz"):
        ext = ".tar.gz"
    threshold = SIZE_THRESHOLDS.get(ext)
    if threshold and result["size"] < threshold:
        result["suspicious"] = True
        result["reason"] = (
            f"文件似乎不完整: {path} ({_format_size(result['size'])})"
            f" — 通常应 ≥ {_format_size(threshold)}"
        )

    # Check if it's an HTML error page (LFS redirect failure, etc.)
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


def _format_size(size_bytes: int) -> str:
    """Format byte size to human-readable string."""
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024**3):.1f}GB"
    elif size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024**2):.0f}MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f}KB"
    return f"{size_bytes}B"


# ── Hook ──────────────────────────────────────────────────────────────

# Keywords for description-based detection
_DESC_KEYWORDS = [
    "download", "下载", "curl", "wget",
    "pip install", "pip3 install",
    "git clone", "clone", "pull", "fetch",
]


def _on_transform_tool_result(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    tool_call_id: str = "",
    **kwargs: Any,
) -> Optional[str]:
    """Append download verification warnings to terminal tool result.

    This is the ONLY hook that can modify what the model sees —
    post_tool_call return values are discarded by Hermes.
    """
    if tool_name != "terminal":
        return None
    if not isinstance(args, dict) or not isinstance(result, str):
        return None

    command = args.get("command", "")
    description = args.get("description", "")
    if not isinstance(command, str):
        return None

    # Only check downloads
    if not _is_download_command(command, str(description)):
        return None

    # Parse tool result
    try:
        result_data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None

    warnings: list[str] = []

    # 1. Check stderr for error signals
    stderr = result_data.get("stderr", "")
    if isinstance(stderr, str) and stderr.strip():
        for sig in ERROR_SIGNALS:
            if sig in stderr:
                warnings.append(
                    f"⚠️ 下载可能失败: stderr 包含错误信号\n"
                    f"```\n{stderr[:300]}\n```"
                )
                break

    # 2. Try to find and verify the output file
    workdir = args.get("workdir", "") or os.getcwd()
    output_path = _extract_output_path(command, str(workdir))

    if output_path:
        validity = _check_output_validity(output_path)
        if validity["suspicious"]:
            warnings.append(f"⚠️ 怀疑下载不完整: {validity['reason']}")

    if not warnings:
        return None

    # Inject audit into JSON structure. Appending after the brace gets swallowed.
    if isinstance(result_data, dict):
        result_data["_audit"] = "\n".join(warnings)
        return json.dumps(result_data, ensure_ascii=False)
    return result + "\n\n---\n" + "\n\n".join(warnings)


def register(ctx) -> None:
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
