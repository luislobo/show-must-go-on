# show-must-go-on

Recover and relaunch your AI coding sessions after a reboot.

If you run multiple terminals with AI coding agents (Claude Code, Codex) and lose them to a restart, crash, or reboot — this tool scans their session history on disk, figures out what you were working on, and reopens everything as Terminator windows for i3 tiling.

## The problem

You have 15 terminals open. Each one has a Claude Code or Codex session deep into a task. You reboot. All gone. You have no idea what you were doing, where you left off, or which sessions to resume.

## The solution

```
restore              # scan, summarize, pick, done
```

`restore` reads the JSONL session files that Claude Code and Codex already persist to disk, scores them by activity, optionally generates AI topic/abstract summaries (cached so subsequent runs are instant), and lets you pick which ones to relaunch — each in its own Terminator window, ready for i3 to tile.

## Features

- **Session discovery** — scans `~/.claude/projects/` and `~/.codex/sessions/` for all session history
- **Smart filtering** — configurable lookback window, minimum activity thresholds, dead-cwd detection
- **AI summaries** — calls `claude -p` to generate a topic label + abstract per session, grounded in the project's README/CLAUDE.md and the last hour of transcript
- **Persistent cache** — summaries are saved to `~/.ai/sessions.jsonl`; subsequent runs reuse them unless the session has new activity
- **Content search** — `--find` searches all session transcripts for a branch name, repo, Jira ticket, or any regex
- **Relevance ranking** — search results ranked by match count, recency, user-message weight, and git branch match
- **Curses TUI picker** — keyboard-driven session selector with previews (j/k, space, enter, a/n/i/q)
- **One Terminator window per session** — `terminator -u` (no-dbus) so i3 sees each as a separate window to tile
- **Git metadata** — resolves each session's working directory to its git remote and current branch
- **Zero dependencies** — Python 3.10+ stdlib only; no pip install needed

## Requirements

- **Python 3.10+**
- **Terminator** terminal emulator (for `--execute`)
- **Claude Code** and/or **Codex** CLI (the tools whose sessions you want to recover)
- **`claude` CLI on PATH** (optional, for AI summaries — works without it, you just get raw prompt previews instead)
- **Linux** with a graphical session (X11/Wayland) for launching Terminator windows
- **git** (optional, for git remote/branch display)

## Installation

```bash
# Clone
git clone https://github.com/luislobo/show-must-go-on.git
cd show-must-go-on

# Symlink to somewhere on your PATH
ln -s "$(pwd)/restore_terminals.py" ~/bin/restore
ln -s "$(pwd)/find_session.py" ~/bin/find-session
```

That's it. No `pip install`, no virtualenv, no build step.

## Usage

### Restore sessions

```bash
# Preview what would be restored (last 7 days, TUI picker, AI summaries)
restore

# Actually launch the Terminator windows
restore --execute

# Look back further
restore 14

# Regenerate all AI summaries from scratch
restore --refresh

# Skip TUI, print everything to stdout
restore --no-pick
```

### Search sessions

```bash
# Find sessions that mention a branch
restore --find feature/my-branch

# Find by Jira ticket
restore --find DSBF-491

# Find by repo name
restore --find twenty20solutions/platform

# Search + launch matching sessions
restore --find feature/my-branch --execute

# Search with a wider time window
restore --find 'haproxy.*migration' 30
```

### Standalone search (no TUI, just results)

```bash
find-session feature/my-branch
find-session DSBF-491 14
```

### TUI controls

| Key | Action |
|-----|--------|
| `j` / `k` / arrows | Move up/down |
| `space` | Toggle checkbox |
| `enter` | Confirm selection (if nothing checked, picks current item) |
| `a` | Select all |
| `n` | Select none |
| `i` | Invert selection |
| `PgUp` / `PgDn` | Scroll |
| `g` / `G` | Jump to top/bottom |
| `q` / `ESC` | Cancel |

The footer shows the AI abstract (or first match snippet in search mode) for the highlighted session.

## How it works

1. **Scan** — walks `~/.claude/projects/*/*.jsonl` and `~/.codex/sessions/**/rollout-*.jsonl`
2. **Parse** — extracts timestamps, message counts, user prompts, cwd, and interrupted status from each JSONL file
3. **Filter** — removes sessions outside the lookback window, below activity thresholds, or with dead cwds
4. **Rank** — by last activity (normal mode) or by relevance score (search mode)
5. **Summarize** — feeds project context (README.md, CLAUDE.md, etc.) + last-hour transcript to `claude -p`, caches the result
6. **Pick** — curses TUI for interactive selection
7. **Launch** — spawns `terminator -u --working-directory <cwd> --title <topic> -x bash -lc '<tool> --resume <id>; exec $SHELL'` per session

Each Terminator window:
- Starts in the session's working directory
- Has the AI-generated topic as its window title (useful for i3 `for_window` rules)
- Resumes the AI agent session
- Falls back to a shell when the agent exits

## File layout

```
~/.claude/projects/           # Claude Code session storage (read-only)
~/.codex/sessions/            # Codex session storage (read-only)
~/.ai/sessions.jsonl          # Summary cache (written by restore)
~/bin/restore                 # Symlink to restore_terminals.py
~/bin/find-session            # Symlink to find_session.py
```

## License

MIT
