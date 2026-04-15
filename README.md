# show-must-go-on

Recover and relaunch your AI coding sessions after a reboot.

If you run multiple terminals with AI coding agents (Claude Code, Codex) and lose them to a restart, crash, or reboot — this tool scans their session history on disk, figures out what you were working on, and reopens everything as Terminator windows on the correct i3 workspaces.

## The problem

You have 15 terminals open across 5 i3 workspaces. Each one has a Claude Code or Codex session deep into a task. You reboot. All gone. You have no idea what you were doing, where you left off, or which sessions to resume — let alone which workspace each belonged to.

## The solution

```
restore --save           # snapshot what's running right now
# ... reboot ...
restore --load --execute # reopen everything, right where it was
```

Or, without a prior snapshot:

```
restore --execute        # recover from session history on disk
```

## Features

- **Session discovery** — scans `~/.claude/projects/` and `~/.codex/sessions/` for all session history
- **Live snapshot** (`--save`) — detects running claude/codex sessions via `/proc`, captures their session IDs, cwds, **i3 workspace names**, and **window titles**
- **Snapshot restore** (`--load`) — reopens sessions from a snapshot, skips any that are already running, places each window on its original i3 workspace
- **Smart filtering** — configurable lookback window, minimum activity thresholds, dead-cwd detection
- **AI summaries** — calls `claude -p` to generate a topic label + abstract per session, grounded in the project's README/CLAUDE.md and the last hour of transcript
- **Persistent cache** — summaries are saved to `~/.ai/sessions.jsonl`; subsequent runs reuse them unless the session has new activity
- **Content search** — `--find` searches all session transcripts for a branch name, repo, Jira ticket, or any regex
- **Relevance ranking** — search results ranked by match count, recency, user-message weight, and git branch match
- **Curses TUI picker** — keyboard-driven session selector with previews (j/k, space, enter, a/n/i/q)
- **i3 workspace-aware** — saves and restores the exact i3 workspace (including custom names like `" 1 "`) for each session
- **One Terminator window per session** — `terminator -u` (no-dbus) so i3 sees each as a separate window to tile
- **Duplicate detection** — `--load` checks `/proc` for already-running sessions and skips them
- **Git metadata** — resolves each session's working directory to its git remote and current branch
- **Zero non-stdlib dependencies** — Python 3.10+ stdlib only; no pip install needed

## Requirements

- **Python 3.10+**
- **i3 window manager** — required for workspace detection and window placement (`i3-msg`)
- **wmctrl** — required for mapping process PIDs to X11 windows and workspaces
- **Terminator** terminal emulator — required for `--execute` (uses `terminator -u` for standalone windows)
- **Claude Code** and/or **Codex** CLI — the tools whose sessions you want to recover
- **Linux with X11** — `/proc` scanning for live detection, `xdotool`/`wmctrl` for window management
- **`claude` CLI on PATH** — optional, for AI summaries (works without it, you just get raw prompt previews)
- **git** — optional, for git remote/branch display

### Install requirements (Debian/Ubuntu)

```bash
sudo apt install i3 terminator wmctrl
```

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

### Save + restore (recommended workflow)

```bash
# Save a snapshot of all running sessions (run before reboot, or cron it)
restore --save

# After reboot: restore everything to the right workspaces
restore --load --execute

# If all sessions are already running, --load does nothing
restore --load --execute
#=> "skipped 10 already-running session(s)"
```

### Recover from history (no prior snapshot)

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

### Normal mode (from history)

1. **Scan** — walks `~/.claude/projects/*/*.jsonl` and `~/.codex/sessions/**/rollout-*.jsonl`
2. **Parse** — extracts timestamps, message counts, user prompts, cwd, and interrupted status from each JSONL file
3. **Filter** — removes sessions outside the lookback window, below activity thresholds, or with dead cwds
4. **Rank** — by last activity (normal mode) or by relevance score (search mode)
5. **Summarize** — feeds project context (README.md, CLAUDE.md, etc.) + last-hour transcript to `claude -p`, caches the result
6. **Pick** — curses TUI for interactive selection
7. **Launch** — spawns `terminator -u --working-directory <cwd> --title <topic> -x bash -lc '<tool> --resume <id>; exec $SHELL'` per session

### Snapshot mode (--save / --load)

1. **Save** — scans `/proc` for running `claude`/`codex` processes, resolves each to its session ID (via `--resume` args, `/proc/PID/fd`, or cwd-to-project mapping), captures the i3 workspace name and window title via `i3-msg -t get_tree` + `wmctrl -lp`
2. **Load** — reads the snapshot, checks which sessions are already running (skips those), parses the JSONL files for metadata, runs through the same summarize/pick/launch pipeline
3. **Launch** — spawns each Terminator window, then uses `i3-msg '[id=<wid>] move to workspace <name>'` to place it on the saved workspace

### Window placement

Each Terminator window:
- Starts in the session's working directory
- Has the AI-generated topic as its window title (useful for i3 `for_window` rules)
- Gets moved to its saved i3 workspace (exact name match, including spaces)
- Resumes the AI agent session
- Falls back to a shell when the agent exits

## File layout

```
~/.claude/projects/           # Claude Code session storage (read-only)
~/.codex/sessions/            # Codex session storage (read-only)
~/.ai/sessions.jsonl          # Summary cache (written by restore)
~/.ai/snapshots/              # Saved snapshots from --save
~/.ai/snapshots/latest.json   # Symlink to most recent snapshot
~/bin/restore                 # Symlink to restore_terminals.py
~/bin/find-session            # Symlink to find_session.py
```

## License

MIT
