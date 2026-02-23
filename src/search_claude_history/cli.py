"""Search across Claude Code session history for keywords/patterns in messages.

Uses rg for fast matching across JSONL files, then Python for parsing and formatting.

Usage (via uvx):
    search-claude-history "git-log-list"
    search-claude-history "git-log-list" --project scripts
    search-claude-history "git-log-list" --type user
    search-claude-history "git-log-list" -B 2 -A 2
    search-claude-history "make_key" --tools
    sch "git-log-list"  # short alias
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude" / "projects"

# Whether to emit ANSI escapes — resolved at startup
_use_color = True


def _init_color(force_no_color=False):
    """Set up color support. Call once at startup."""
    global _use_color

    if force_no_color or os.environ.get("NO_COLOR"):
        _use_color = False
        return

    if not sys.stdout.isatty():
        _use_color = False
        return

    # Enable ANSI/VT processing on Windows 10+
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # STD_OUTPUT_HANDLE = -11, ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x4
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            kernel32.SetConsoleMode(handle, mode.value | 0x4)
        except Exception:
            _use_color = False


def _sgr(code):
    """Return an ANSI SGR sequence if color is enabled, else empty string."""
    return f"\033[{code}m" if _use_color else ""


def DIM():
    return _sgr("2")


def BOLD_RED():
    return _sgr("1;31")


def CYAN():
    return _sgr("36")


def GREEN():
    return _sgr("32")


def YELLOW():
    return _sgr("33")


def RESET():
    return _sgr("0")


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
    role = msg.get("role") or ltype

    # User lines with plain string content
    content = msg.get("content", "")
    if isinstance(content, str) and content and ltype == "user":
        return ("user", "user", content)

    if isinstance(content, list):
        # Thinking-only blocks — skip
        if all(b.get("type") == "thinking" for b in content):
            return None

        content_types = {b.get("type") for b in content}
        is_tool = bool(content_types & {"tool_use", "tool_result"})
        has_text = "text" in content_types
        if is_tool and not has_text:
            return ("tool", role, extract_text(msg))
        elif has_text or is_tool:
            text = extract_text(msg)
            return (role if role in ("user", "assistant") else ltype, role, text)

    return None


def read_lines_from_file(filepath, line_numbers):
    """Read specific 1-indexed line numbers from a file."""
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
            f"{BOLD_RED()}\\1{RESET()}",
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
    """Extract and format timestamp from a JSONL object."""
    ts = obj.get("timestamp", "")
    if ts:
        return ts[:16].replace("T", " ")
    return ""


# Regex for parsing rg output: filepath:lineno:content
# Works with Windows drive letters (C:\...) because \d+ constrains the line-number group.
_RG_LINE_RE = re.compile(r"^(.+?):(\d+):(.*)$", re.DOTALL)


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
        print(
            "Error: rg (ripgrep) not found. Install it from https://github.com/BurntSushi/ripgrep",
            file=sys.stderr,
        )
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("Error: rg timed out after 60s", file=sys.stderr)
        sys.exit(1)

    matches = []
    for line in result.stdout.splitlines():
        m = _RG_LINE_RE.match(line)
        if not m:
            continue
        filepath, lineno_str, content = m.group(1), m.group(2), m.group(3)
        lineno = int(lineno_str)

        if project_filter:
            project, _ = parse_session_info(filepath)
            if project_filter.lower() not in project.lower():
                continue

        matches.append((filepath, lineno, content))

    return matches


def format_role(role):
    if role == "user":
        return f"{GREEN()}[user]{RESET()}"
    elif role == "assistant":
        return f"{CYAN()}[assistant]{RESET()}"
    else:
        return f"{YELLOW()}[{role}]{RESET()}"


def main():
    parser = argparse.ArgumentParser(
        description="Search Claude Code session history",
    )
    parser.add_argument("pattern", help="Search pattern (passed to rg, supports regex)")
    parser.add_argument("-p", "--project", help="Filter to project (substring match on dir name)")
    parser.add_argument(
        "-t", "--type",
        choices=["user", "assistant"],
        help="Filter by message role",
    )
    parser.add_argument("-A", type=int, default=0, metavar="N", help="Show N messages after match")
    parser.add_argument("-B", type=int, default=0, metavar="N", help="Show N messages before match")
    parser.add_argument(
        "--tools", action="store_true",
        help="Include tool_use/tool_result messages (skipped by default)",
    )
    parser.add_argument(
        "-C", "--context", type=int, default=200, metavar="CHARS",
        help="Chars of text context around match (default 200)",
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Disable colored output",
    )
    args = parser.parse_args()

    _init_color(force_no_color=args.no_color)

    if not CLAUDE_DIR.exists():
        print(f"Error: {CLAUDE_DIR} not found", file=sys.stderr)
        sys.exit(1)

    matches = run_rg(args.pattern, args.project)
    if not matches:
        print("No matches found.")
        return

    printed_count = 0
    seen = set()

    for filepath, lineno, raw_line in matches:
        if (filepath, lineno) in seen:
            continue
        seen.add((filepath, lineno))

        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        info = classify_line(obj)
        if info is None:
            continue

        msg_type, role, text = info

        if msg_type == "tool" and not args.tools:
            continue
        if args.type and role != args.type:
            continue

        # Surrounding context lines
        before_lines = []
        after_lines = []
        if args.B > 0 or args.A > 0:
            want = set()
            for offset in range(-args.B, 0):
                ln = lineno + offset
                if ln >= 1:
                    want.add(ln)
            for offset in range(1, args.A + 1):
                want.add(lineno + offset)

            if want:
                raw_lines = read_lines_from_file(filepath, want)
                for ln in sorted(want):
                    if ln in raw_lines:
                        try:
                            o = json.loads(raw_lines[ln])
                            ci = classify_line(o)
                            if ci and (ci[0] != "tool" or args.tools):
                                if ln < lineno:
                                    before_lines.append(ci)
                                else:
                                    after_lines.append(ci)
                        except json.JSONDecodeError:
                            pass

        # Header
        project, session_id = parse_session_info(filepath)
        timestamp = get_timestamp(obj)
        short_session = session_id[:8] if session_id else "?"
        header = f"{DIM()}── {timestamp} | {project} | session:{short_session} ──{RESET()}"
        if printed_count > 0:
            print()
        print(header)

        # Before context (dimmed)
        for _, r, t in before_lines:
            t_short = truncate_around_match(t, args.pattern, args.context).replace("\n", " ")
            print(f"  {DIM()}{format_role(r)} {t_short}{RESET()}")

        # Matching line (highlighted)
        text_short = truncate_around_match(text, args.pattern, args.context).replace("\n", " ")
        text_highlighted = highlight_pattern(text_short, args.pattern)
        print(f"  {format_role(role)} {text_highlighted}")

        # After context (dimmed)
        for _, r, t in after_lines:
            t_short = truncate_around_match(t, args.pattern, args.context).replace("\n", " ")
            print(f"  {DIM()}{format_role(r)} {t_short}{RESET()}")

        printed_count += 1

    if printed_count == 0:
        print("No matching messages found (results were all filtered out).")
    else:
        print(f"\n{DIM()}{printed_count} match(es){RESET()}")


if __name__ == "__main__":
    main()
