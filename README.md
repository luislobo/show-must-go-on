# show-must-go-on

Recover and relaunch your AI coding sessions after a reboot.

If you run multiple terminals with AI coding agents (Claude Code, Codex) and lose them to a restart, crash, or reboot, this tool scans their session history on disk, figures out what you were working on, and reopens everything as Terminator windows on the correct i3 workspaces. For remote or headless work, it can also rebuild the selected sessions as tmux/byobu panes.

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
- **Byobu/tmux backend** — `--target=byobu` rebuilds sessions into panes/windows for SSH or non-GUI machines
- **Duplicate detection** — `--load` checks `/proc` for already-running sessions and skips them
- **Git metadata** — resolves each session's working directory to its git remote and current branch
- **GitHub PR metadata** — when `gh` is installed and authenticated, shows PR number, state, title, and URL for matching branches
- **Fast search prefilter** — when `rg` is installed, `--find` narrows candidate JSONL files before Python parsing
- **Zero non-stdlib dependencies** — Python 3.10+ stdlib only; no pip install needed

## Requirements

- **Python 3.10+** — no pip packages required
- **Claude Code** and/or **Codex** CLI — the tools whose sessions you want to recover
- **Linux** — session discovery and duplicate detection read `/proc`

For the default GUI backend:

- **i3 window manager** — required for workspace detection and window placement (`i3-msg`)
- **wmctrl** — required for mapping process PIDs to X11 windows and workspaces
- **Terminator** — required for `restore --execute` with the default `--target=terminator`
- **X11 session** — required for i3/wmctrl window placement

For remote/headless use:

- **tmux** or **byobu** — required for `restore --target=byobu --execute`

Optional tools:

- **claude** — AI summaries via `claude -p`; without it, restore still works with raw prompt previews
- **git** — git remote/branch display
- **gh** — GitHub PR metadata; run `gh auth login` first
- **ripgrep** (`rg`) — faster `--find` pre-filtering

### Install requirements (Debian/Ubuntu)

```bash
# Full local desktop setup
sudo apt install python3 git i3 terminator wmctrl ripgrep gh byobu tmux

# Minimal SSH/headless setup for --target=byobu
sudo apt install python3 git ripgrep gh byobu tmux
```

## Installation

```bash
# Clone
git clone https://github.com/luislobo/show-must-go-on.git
cd show-must-go-on

# Install command symlinks into ~/bin
./install.sh

# If ~/bin is not already on PATH, add it to your shell profile
export PATH="$HOME/bin:$PATH"
```

Manual install is just two symlinks:

```bash
mkdir -p ~/bin
ln -sfn "$(pwd)/restore_terminals.py" ~/bin/restore
ln -sfn "$(pwd)/find_session.py" ~/bin/find-session
```

That's it. No `pip install`, no virtualenv, no build step.

### Verify installation

```bash
restore --help
find-session --help
python3 -m py_compile restore_terminals.py find_session.py
```

### Moving to another computer

The repo contains the scripts and installer, but it does not contain your private AI session history. On the new computer, install Claude Code and/or Codex first, then clone this repo and run `./install.sh`.

To restore sessions from this computer on another one, copy the relevant session data too:

```bash
rsync -a ~/.claude/projects/ user@new-host:~/.claude/projects/
rsync -a ~/.codex/sessions/ user@new-host:~/.codex/sessions/
rsync -a ~/.ai/snapshots/ user@new-host:~/.ai/snapshots/
```

`~/.ai/sessions.jsonl` is only a summary cache. Copying it is optional; the tool can regenerate summaries.

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

### Remote/headless restore with byobu/tmux

```bash
# Preview the tmux/byobu plan
restore --target=byobu

# Build a tmux session named "restore"; attach after it prints the command
restore --target=byobu --execute

# Put each restored session in its own tmux window
restore --target=byobu --layout=windows --execute

# Put up to six panes in each tmux window
restore --target=byobu --layout=hybrid --max-splits=6 --execute
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

### Byobu/tmux placement

With `--target=byobu`, restore builds a tmux session instead of GUI windows:

- Default session name is `restore`; override with `--byobu-session`
- `--layout=hybrid` puts up to `--max-splits` panes in a window, then creates another window
- `--layout=splits` puts all selected sessions in one window
- `--layout=windows` creates one tmux window per session
- Pane/window names use AI-generated slugs when summaries are available

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
