# search-claude-history

Search across all your [Claude Code](https://docs.anthropic.com/en/docs/claude-code) session history for keywords or patterns.

```
$ sch "git-log-list"
── 2026-02-17 05:35 | -code-rb | session:720d82ef ──
  [assistant] There it is — 114 commits, numbered and dated. Usage: `./scripts/git-log-list.sh`...

── 2026-02-23 20:15 | -code-scripts | session:379bb145 ──
  [user] do you remember the motivation behind git-log-list.sh? search our session history

2 match(es)
```

Works out of the box, but fastest with [ripgrep](https://github.com/BurntSushi/ripgrep) installed.

## Install

```bash
# Run directly (no install needed)
uvx search-claude-history "pattern"

# Or install globally
uv tool install search-claude-history

# With hyperscan for faster searches (matches ripgrep speed)
uv tool install "search-claude-history[hyperscan]"
```

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/). No system Python needed.

## Usage

```
sch <pattern> [options]
```

| Flag | Description |
|------|-------------|
| `-p`, `--project` | Filter to a project (substring match on dir name) |
| `-t`, `--type` | Filter by role: `user` or `assistant` |
| `-A N` | Show N messages after each match |
| `-B N` | Show N messages before each match |
| `-C`, `--context` | Chars of text context around match (default 200) |
| `--tools` | Include tool_use/tool_result messages (hidden by default) |
| `--no-color` | Disable colored output |

### Examples

```bash
sch "database migration"                  # search everything
sch "TypeError" --type assistant          # only assistant messages
sch "deployment" -p myproject             # filter to a project
sch "refactor" -B 1 -A 1                 # show surrounding messages
sch "SELECT.*FROM" --tools               # search tool calls too
```

## Performance

Searches `~/.claude/projects/` using the fastest available engine:

| Engine | 0.45 GB | Requires |
|--------|---------|----------|
| [ripgrep](https://github.com/BurntSushi/ripgrep) | 0.13s | `rg` on PATH |
| [hyperscan](https://pypi.org/project/hyperscan/) | 0.15s | `pip install search-claude-history[hyperscan]` |
| stdlib (mmap + multiprocessing) | 0.64s | nothing |

The tool uses whichever engine is available, in the order above.
