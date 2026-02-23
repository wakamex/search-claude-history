"""Search across Claude Code session history for keywords/patterns in messages.

Uses rg for fast matching when available, falls back to pure Python otherwise.

Usage (via uvx):
    search-claude-history "git-log-list"
    search-claude-history "git-log-list" --project scripts
    search-claude-history "git-log-list" --type user
    search-claude-history "git-log-list" -B 2 -A 2
    search-claude-history "make_key" --tools
    sch "git-log-list"  # short alias
"""

import argparse
import concurrent.futures
import json
import mmap
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"


def _get_version():
    try:
        from importlib.metadata import version
        return version("search-claude-history")
    except Exception:
        pass
    # Fallback: read version from pyproject.toml + git hash
    base = "0.0.0"
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            tomllib = None
    if tomllib:
        toml_path = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
        try:
            with open(toml_path, "rb") as f:
                base = tomllib.load(f).get("project", {}).get("version", base)
        except OSError:
            pass
    git_hash = ""
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=5,
            cwd=Path(__file__).parent,
        )
        if out.returncode == 0 and out.stdout.strip():
            git_hash = out.stdout.strip()
    except Exception:
        pass
    return f"{base}+{git_hash}" if git_hash else base


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


def _find_jsonl_files(project_filter=None):
    """Walk CLAUDE_DIR for session JSONL files, excluding subagents/."""
    for dirpath, dirnames, filenames in os.walk(CLAUDE_DIR):
        # Prune subagents directories
        dirnames[:] = [d for d in dirnames if d != "subagents"]
        for fname in filenames:
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(dirpath, fname)
            if project_filter:
                project, _ = parse_session_info(fpath)
                if project_filter.lower() not in project.lower():
                    continue
            yield fpath


def _mmap_extract_lines(mm, size, match_positions):
    """Given a list of (start, end) byte positions in an mmap, extract (lineno, line_text) for each."""
    results = []
    for start, end in match_positions:
        line_start = mm.rfind(b"\n", 0, start) + 1
        line_end = mm.find(b"\n", end)
        if line_end == -1:
            line_end = size
        lineno = mm[:line_start].count(b"\n") + 1
        line_bytes = mm[line_start:line_end]
        results.append((lineno, line_bytes.decode("utf-8", errors="replace")))
    return results


def _search_file_hs(args):
    """Search a single file using hyperscan + mmap. Runs in a worker process."""
    fpath, pattern_str = args
    import hyperscan as hs

    db = hs.Database()
    try:
        db.compile(
            expressions=[pattern_str.encode("utf-8", errors="replace")],
            flags=[hs.HS_FLAG_CASELESS | hs.HS_FLAG_SOM_LEFTMOST],
        )
    except hs.error:
        db.compile(
            expressions=[re.escape(pattern_str).encode("utf-8", errors="replace")],
            flags=[hs.HS_FLAG_CASELESS | hs.HS_FLAG_SOM_LEFTMOST],
        )

    positions = []

    def on_match(id, start, end, flags, context):
        positions.append((start, end))

    matches = []
    try:
        with open(fpath, "rb") as f:
            size = f.seek(0, 2)
            if size == 0:
                return matches
            f.seek(0)
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                db.scan(bytes(mm), match_event_handler=on_match)
                if positions:
                    for lineno, line_text in _mmap_extract_lines(mm, size, positions):
                        matches.append((fpath, lineno, line_text))
    except OSError:
        pass
    return matches


def _search_file_re(args):
    """Search a single file using mmap + re. Runs in a worker process."""
    fpath, pattern_str = args
    try:
        regex_b = re.compile(pattern_str.encode("utf-8", errors="replace"), re.IGNORECASE)
    except re.error:
        regex_b = re.compile(re.escape(pattern_str).encode("utf-8", errors="replace"), re.IGNORECASE)

    matches = []
    try:
        with open(fpath, "rb") as f:
            size = f.seek(0, 2)
            if size == 0:
                return matches
            f.seek(0)
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                positions = [(m.start(), m.end()) for m in regex_b.finditer(mm)]
                if positions:
                    for lineno, line_text in _mmap_extract_lines(mm, size, positions):
                        matches.append((fpath, lineno, line_text))
    except OSError:
        pass
    return matches


def _has_hyperscan():
    try:
        import hyperscan  # noqa: F401
        return True
    except ImportError:
        return False


def _search_python(pattern, project_filter=None):
    """Python fallback: hyperscan if installed, else mmap+re. Both multiprocessed."""
    files = list(_find_jsonl_files(project_filter))
    if not files:
        return []

    use_hs = _has_hyperscan()
    worker_fn = _search_file_hs if use_hs else _search_file_re
    if not use_hs:
        print(
            f"{DIM()}(tip: pip install hyperscan for ~4x faster searches){RESET()}",
            file=sys.stderr,
        )

    work = [(f, pattern) for f in files]
    all_matches = []

    workers = min(os.cpu_count() or 1, len(files))
    if workers <= 1:
        for w in work:
            all_matches.extend(worker_fn(w))
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
            for file_matches in pool.map(worker_fn, work, chunksize=4):
                all_matches.extend(file_matches)

    return all_matches


# Regex for parsing rg output: filepath:lineno:content
# Works with Windows drive letters (C:\...) because \d+ constrains the line-number group.
_RG_LINE_RE = re.compile(r"^(.+?):(\d+):(.*)$", re.DOTALL)


def _search_rg(pattern, project_filter=None):
    """Use rg for fast searching. Returns None if rg is not available."""
    if not shutil.which("rg"):
        return None

    cmd = [
        "rg", "--no-heading", "-n", "-i",
        "--glob", "*.jsonl",
        "--glob", "!**/subagents/**",
        pattern,
        str(CLAUDE_DIR),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None

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


def search(pattern, project_filter=None):
    """Search session files. Tries rg first, then hyperscan, then mmap+re."""
    result = _search_rg(pattern, project_filter)
    if result is not None:
        return result
    return _search_python(pattern, project_filter)


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
    parser.add_argument(
        "-V", "--version", action="version",
        version=f"%(prog)s {_get_version()}",
    )
    parser.add_argument("pattern", help="Search pattern (supports regex)")
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

    matches = search(args.pattern, args.project)
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
