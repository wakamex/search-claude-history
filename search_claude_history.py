#!/usr/bin/env python3
"""Search across Claude Code session history for keywords/patterns in messages.

Uses rg for fast matching across JSONL files, then Python for parsing and formatting.

Usage:
    python search_claude_history.py "git-log-list"
    python search_claude_history.py "git-log-list" --project scripts
    python search_claude_history.py "git-log-list" --type user
    python search_claude_history.py "git-log-list" -B 2 -A 2
    python search_claude_history.py "make_key" --tools
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude" / "projects"

# ANSI
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RESET = "\033[0m"
BOLD_RED = "\033[1;31m"


def extract_text(message_obj):
    """Extract readable text from a JSONL line's message content."""
    content = message_obj.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                # Show a compact summary
                if name in ("Bash", "Read", "Write", "Edit", "Glob", "Grep"):
                    detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                    if len(detail) > 120:
                        detail = detail[:120] + "..."
                    parts.append(f"[{name}: {detail}]")
                else:
                    parts.append(f"[{name}]")
            elif btype == "tool_result":
                result = block.get("content", "")
                if isinstance(result, str):
                    parts.append(result)
                elif isinstance(result, list):
                    for r in result:
                        if r.get("type") == "text":
                            parts.append(r.get("text", ""))
        return "\n".join(parts)
    return ""


def classify_line(obj):
    """Return (type, role, text) for a parsed JSONL line, or None to skip."""
    ltype = obj.get("type")
    if ltype in ("file-history-snapshot", "progress"):
        return None

    msg = obj.get("message", {})
    role = msg.get("role") or ltype  # fallback to type field

    # For user lines with string content (direct user messages)
    content = msg.get("content", "")
    if isinstance(content, str) and content and ltype == "user":
        return ("user", "user", content)

    if isinstance(content, list):
        content_types = {b.get("type") for b in content}
        # Determine if this is a tool line
        is_tool = bool(content_types & {"tool_use", "tool_result"})
        # Determine if there's readable text
        has_text = "text" in content_types
        if is_tool and not has_text:
            text = extract_text(msg)
            return ("tool", role, text)
        elif has_text or is_tool:
            text = extract_text(msg)
            return (role if role in ("user", "assistant") else ltype, role, text)

    # thinking-only blocks
    if isinstance(content, list) and all(b.get("type") == "thinking" for b in content):
        return None

    return None


def read_lines_from_file(filepath, line_numbers):
    """Read specific 1-indexed line numbers from a file efficiently."""
    needed = set(line_numbers)
    results = {}
    with open(filepath, "r", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if i in needed:
                results[i] = line
                if len(results) == len(needed):
                    break
    return results


def highlight_pattern(text, pattern, ignore_case=True):
    """Highlight pattern matches in text with bold red."""
    try:
        flags = re.IGNORECASE if ignore_case else 0
        return re.sub(
            f"({re.escape(pattern)})",
            f"{BOLD_RED}\\1{RESET}",
            text,
            flags=flags,
        )
    except re.error:
        return text


def truncate_around_match(text, pattern, context_chars):
    """Truncate text to show context_chars around the first match."""
    if context_chars <= 0:
        return text
    try:
        m = re.search(re.escape(pattern), text, re.IGNORECASE)
    except re.error:
        m = None
    if not m:
        return text[:context_chars * 2] + ("..." if len(text) > context_chars * 2 else "")
    start = max(0, m.start() - context_chars)
    end = min(len(text), m.end() + context_chars)
    result = ""
    if start > 0:
        result += "..."
    result += text[start:end]
    if end < len(text):
        result += "..."
    return result


def parse_session_info(filepath):
    """Extract project name and session ID from filepath."""
    parts = Path(filepath).parts
    # .claude/projects/<project>/<session>.jsonl
    project = ""
    session_id = ""
    for i, p in enumerate(parts):
        if p == "projects" and i + 1 < len(parts):
            project = parts[i + 1]
            if i + 2 < len(parts):
                session_id = Path(parts[i + 2]).stem
            break
    return project, session_id


def get_timestamp(obj):
    """Extract timestamp string from a JSONL object."""
    ts = obj.get("timestamp", "")
    if ts:
        # "2026-02-23T16:32:11.149Z" -> "2026-02-23 16:32"
        return ts[:16].replace("T", " ")
    return ""


def run_rg(pattern, project_filter=None):
    """Run rg across session JSONL files. Returns list of (filepath, lineno, raw_line)."""
    search_path = str(CLAUDE_DIR)
    cmd = [
        "rg", "--no-heading", "-n", "-i",
        "--glob", "*.jsonl",
        "--glob", "!**/subagents/**",
        pattern,
        search_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        print("Error: rg (ripgrep) not found. Please install it.", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("Error: rg timed out after 60s", file=sys.stderr)
        sys.exit(1)

    matches = []
    for line in result.stdout.splitlines():
        # Format: filepath:lineno:content
        # Need to handle filepath containing colons (unlikely on Linux but be safe)
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        filepath, lineno_str, content = parts[0], parts[1], parts[2]
        try:
            lineno = int(lineno_str)
        except ValueError:
            continue

        if project_filter:
            project, _ = parse_session_info(filepath)
            if project_filter.lower() not in project.lower():
                continue

        matches.append((filepath, lineno, content))

    return matches


def format_role(role):
    if role == "user":
        return f"{GREEN}[user]{RESET}"
    elif role == "assistant":
        return f"{CYAN}[assistant]{RESET}"
    else:
        return f"{YELLOW}[{role}]{RESET}"


def main():
    parser = argparse.ArgumentParser(
        description="Search Claude Code session history"
    )
    parser.add_argument("pattern", help="Search pattern (passed to rg)")
    parser.add_argument("-p", "--project", help="Filter to project (substring match)")
    parser.add_argument(
        "-t", "--type",
        choices=["user", "assistant"],
        help="Filter by message role",
    )
    parser.add_argument("-A", type=int, default=0, metavar="N", help="Show N messages after match")
    parser.add_argument("-B", type=int, default=0, metavar="N", help="Show N messages before match")
    parser.add_argument(
        "--tools", action="store_true",
        help="Also search tool_use/tool_result lines (skipped by default)",
    )
    parser.add_argument(
        "-C", "--context", type=int, default=200, metavar="CHARS",
        help="Chars of text context around match (default 200)",
    )
    args = parser.parse_args()

    if not CLAUDE_DIR.exists():
        print(f"Error: {CLAUDE_DIR} not found", file=sys.stderr)
        sys.exit(1)

    matches = run_rg(args.pattern, args.project)
    if not matches:
        print("No matches found.")
        return

    # Group by file and deduplicate
    # For each match, parse it and optionally fetch surrounding lines
    printed_count = 0
    seen = set()  # (filepath, lineno) to deduplicate

    for filepath, lineno, raw_line in matches:
        if (filepath, lineno) in seen:
            continue
        seen.add((filepath, lineno))

        # Parse the matched line
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        info = classify_line(obj)
        if info is None:
            continue

        msg_type, role, text = info

        # Skip tool lines unless --tools
        if msg_type == "tool" and not args.tools:
            continue

        # Filter by type
        if args.type and role != args.type:
            continue

        # Gather surrounding lines if -A/-B requested
        before_lines = []
        after_lines = []
        if args.B > 0 or args.A > 0:
            # Compute which lines to read
            want = set()
            for offset in range(-args.B, 0):
                ln = lineno + offset
                if ln >= 1:
                    want.add(ln)
            for offset in range(1, args.A + 1):
                want.add(lineno + offset)

            if want:
                raw_lines = read_lines_from_file(filepath, want)
                # Parse before lines
                for ln in sorted(want):
                    if ln >= lineno:
                        continue
                    if ln in raw_lines:
                        try:
                            o = json.loads(raw_lines[ln])
                            ci = classify_line(o)
                            if ci and (ci[0] != "tool" or args.tools):
                                before_lines.append(ci)
                        except json.JSONDecodeError:
                            pass
                # Parse after lines
                for ln in sorted(want):
                    if ln <= lineno:
                        continue
                    if ln in raw_lines:
                        try:
                            o = json.loads(raw_lines[ln])
                            ci = classify_line(o)
                            if ci and (ci[0] != "tool" or args.tools):
                                after_lines.append(ci)
                        except json.JSONDecodeError:
                            pass

        # Print header
        project, session_id = parse_session_info(filepath)
        timestamp = get_timestamp(obj)
        short_session = session_id[:8] if session_id else "?"
        header = f"{DIM}── {timestamp} | {project} | session:{short_session} ──{RESET}"
        if printed_count > 0:
            print()
        print(header)

        # Print before context
        for _, r, t in before_lines:
            t_short = truncate_around_match(t, args.pattern, args.context)
            t_short = t_short.replace("\n", " ")
            print(f"  {DIM}{format_role(r)} {t_short}{RESET}")

        # Print matching line (highlighted)
        text_short = truncate_around_match(text, args.pattern, args.context)
        text_short = text_short.replace("\n", " ")
        text_highlighted = highlight_pattern(text_short, args.pattern)
        print(f"  {format_role(role)} {text_highlighted}")

        # Print after context
        for _, r, t in after_lines:
            t_short = truncate_around_match(t, args.pattern, args.context)
            t_short = t_short.replace("\n", " ")
            print(f"  {DIM}{format_role(r)} {t_short}{RESET}")

        printed_count += 1

    if printed_count == 0:
        print("No matching messages found (results were all filtered out).")
    else:
        print(f"\n{DIM}{printed_count} match(es){RESET}")


if __name__ == "__main__":
    main()
