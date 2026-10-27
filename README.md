# show-must-go-on

Recover and relaunch your AI coding sessions after a reboot.

If you run multiple terminals with AI coding agents (Claude Code, Codex) and lose them to a restart, crash, or reboot, this tool scans their session history on disk, figures out what you were working on, and reopens everything: as Terminator windows on the correct i3 workspaces on Linux, or as Ghostty or Terminal.app windows on macOS. For remote or headless work, it can also rebuild the selected sessions as tmux/byobu panes.

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
- **Live snapshot** (`--save`) — detects running claude/codex sessions (via `/proc` on Linux, `ps` and `lsof` on macOS), captures their session IDs, cwds, **i3 workspace names** or **Ghostty/Terminal.app window and tab**, and **window titles**
- **Exact Claude session IDs** — reads Claude Code's per-process record (`~/.claude/sessions/<pid>.json`) when present, so several sessions in one folder, and sessions after `/clear`, resolve correctly
- **Snapshot restore** (`--load`) — reopens sessions from a snapshot, skips any that are already running, places each window on its original i3 workspace, or (with `--layout=saved`) back in its Ghostty window and tab
- **Smart filtering** — configurable lookback window, minimum activity thresholds, dead-cwd detection
- **AI summaries** — calls `claude -p` to generate a topic label + abstract per session, grounded in the project's README/CLAUDE.md and the last hour of transcript
- **Persistent cache** — summaries are saved to `~/.ai/sessions.jsonl`; subsequent runs reuse them unless the session has new activity
- **Content search** — `--find` searches all session transcripts for a branch name, repo, Jira ticket, or any regex
- **Relevance ranking** — search results ranked by match count, recency, user-message weight, and git branch match
- **Curses TUI picker** — keyboard-driven session selector with previews (j/k, space, enter, a/n/i/q)
- **i3 workspace-aware** (Linux) — saves and restores the exact i3 workspace (including custom names like `" 1 "`) for each session
- **One Terminator window per session** (Linux) — `terminator -u` (no-dbus) so i3 sees each as a separate window to tile
- **Ghostty backend** (macOS) — `--target=ghostty` opens one Ghostty window per session through Ghostty's AppleScript support; splits, tabs, or the saved windows and tabs on request
- **Terminal.app backend** (macOS) — `--target=terminal` opens one Terminal.app window per session
- **Byobu/tmux backend** — `--target=byobu` rebuilds sessions into panes/windows for SSH or non-GUI machines
- **Duplicate detection** — `--load` checks running processes for already-running sessions and skips them
- **Git metadata** — resolves each session's working directory to its git remote and current branch
- **GitHub PR metadata** — when `gh` is installed and authenticated, shows PR number, state, title, and URL for matching branches
- **Fast search prefilter** — when `rg` is installed, `--find` narrows candidate JSONL files before Python parsing
- **Zero non-stdlib dependencies** — Python 3.9+ stdlib only (macOS's `/usr/bin/python3` is enough); no pip install needed

## Requirements

- **Python 3.9+** — no pip packages required
- **Claude Code** and/or **Codex** CLI — the tools whose sessions you want to recover
- **Linux or macOS** — running sessions are found through `/proc` on Linux and `ps`/`lsof` on macOS

`--target` defaults to `auto`: Terminator on Linux; on macOS, the terminal a snapshot was saved from, otherwise the terminal you run `restore` in (Ghostty, else Terminal.app).

For the Linux GUI backend (`--target=terminator`):

- **i3 window manager** — required for workspace detection and window placement (`i3-msg`)
- **wmctrl** — required for mapping process PIDs to X11 windows and workspaces
- **Terminator** — required for `restore --execute` with the default `--target=terminator`
- **X11 session** — required for i3/wmctrl window placement

For the macOS GUI backends:

- **Ghostty 1.3+** — for `--target=ghostty`; earlier versions have no AppleScript support
- **Terminal.app** — for `--target=terminal`; nothing to install
- **Automation permission** — the first launch asks to let your terminal control Ghostty or Terminal (System Settings → Privacy & Security → Automation). `--save` asks the same to read window and tab positions

For remote/headless use (either OS):

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

### Install requirements (macOS, Homebrew)

```bash
# Optional tools; Python 3.9 ships with the Xcode command line tools
brew install ripgrep gh tmux
brew install --cask ghostty     # only for --target=ghostty
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

`install.sh` takes the target directory as its only argument, so you can link into a directory that is already on `PATH`, such as `~/.local/bin`:

```bash
./install.sh ~/.local/bin
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
python3 -m unittest discover tests
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

# After reboot: reopen everything (i3 workspaces on Linux, one window per session on macOS)
restore --load --execute

# If all sessions are already running, --load does nothing
restore --load --execute
#=> "skipped 10 already-running session(s)"
```

### Recover from history (no prior snapshot)

```bash
# Preview what would be restored (last 7 days, TUI picker, AI summaries)
restore

# Actually launch them: Terminator on Linux; on macOS the terminal you are in
# (Ghostty or Terminal.app)
restore --execute

# Look back further
restore 14

# Regenerate all AI summaries from scratch
restore --refresh

# Skip TUI, print everything to stdout
restore --no-pick
```

### macOS: Ghostty and Terminal.app

```bash
# Snapshot running sessions, with their Ghostty or Terminal.app window and tab
restore --save

# Reopen them, one window per session
restore --load --execute

# Reopen Ghostty sessions in their saved windows and tabs
# (sessions that shared a tab become splits)
restore --load --layout=saved --execute

# From history: one Ghostty window per session (the default, no tabs)
restore --target=ghostty --execute

# One Ghostty window with a tab per session
restore --target=ghostty --layout=tabs --execute

# Up to three splits per window, then a new window
restore --target=ghostty --layout=hybrid --max-splits=3 --execute

# One Terminal.app window per session
restore --target=terminal --execute
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

The current row has a `▶` in the gutter and bold text. Colours come from your terminal's own palette, so they follow its light or dark theme: cyan for the tool and the key hints, green for ticked rows, branches and open PRs, and yellow for interrupted sessions (`!`) and search hits. Only the normal ANSI colours are used, never the bright ones, which are hard to read on light backgrounds. The progress lines printed while restore scans, summarizes and searches use the same colours, with the numbers in bold; they stay plain when the output is piped or redirected. Set `NO_COLOR=1` to turn colour off.

## How it works

### Normal mode (from history)

1. **Scan** — walks `~/.claude/projects/*/*.jsonl` and `~/.codex/sessions/**/rollout-*.jsonl`
2. **Parse** — extracts timestamps, message counts, user prompts, cwd, and interrupted status from each JSONL file
3. **Filter** — removes sessions outside the lookback window, below activity thresholds, or with dead cwds
4. **Rank** — by last activity (normal mode) or by relevance score (search mode)
5. **Summarize** — feeds project context (README.md, CLAUDE.md, etc.) + last-hour transcript to `claude -p`, caches the result
6. **Pick** — curses TUI for interactive selection
7. **Launch** — opens each session with the chosen `--target`:
   - `terminator`: spawns `terminator -u --working-directory <cwd> --title <topic> -x bash -lc '<tool> --resume <id>; exec $SHELL'`
   - `ghostty`: creates Ghostty windows (or splits, or tabs) through AppleScript, each starting in `<cwd>` and typing `cd -- <cwd> && <tool> --resume <id>` into your shell
   - `terminal`: runs `cd -- <cwd> && <tool> --resume <id>` with Terminal.app's `do script`, one window per session
   - `byobu`: builds a tmux session (see below)

### Snapshot mode (--save / --load)

1. **Save** — lists running `claude`/`codex` processes (`/proc` on Linux, `ps` + `lsof` on macOS), resolves each to its session ID (via `~/.claude/sessions/<pid>.json`, `--resume` args, the process's open files, or cwd-to-project mapping), and records where it runs: the i3 workspace and window title via `i3-msg -t get_tree` + `wmctrl -lp` on Linux, or the Ghostty / Terminal.app window and tab via AppleScript on macOS
2. **Load** — reads the snapshot, checks which sessions are already running (skips those), parses the JSONL files for metadata, runs through the same summarize/pick/launch pipeline
3. **Launch** — on Linux, spawns each Terminator window, then uses `i3-msg '[id=<wid>] move to workspace <name>'` to place it on the saved workspace. On macOS, opens the sessions in Ghostty or Terminal.app; `--layout=saved` puts Ghostty sessions back in their saved window and tab

Snapshots are JSON files with one entry per session: `tool`, `session_id`, `cwd`, `pid`, `jsonl_path`, `workspace` and `window_title`, plus `terminal` (the host terminal: ghostty, terminal, terminator, tmux or iterm) and, on macOS, `window_id` and `tab_index`. Snapshots saved before the macOS fields existed still load.

### Terminator placement (Linux)

Each Terminator window:
- Starts in the session's working directory
- Has the AI-generated topic as its window title (useful for i3 `for_window` rules)
- Gets moved to its saved i3 workspace (exact name match, including spaces)
- Resumes the AI agent session
- Falls back to a shell when the agent exits

### Ghostty placement (macOS)

With `--target=ghostty`, each session:

- Starts in the session's working directory, in your normal interactive shell
- Resumes the AI agent session by typing the resume command into that shell
- Leaves you at the shell prompt when the agent exits

Layouts:

- `--layout=windows` (default): one window per session, no tabs
- `--layout=splits`: all selected sessions as splits in one window
- `--layout=hybrid`: up to `--max-splits` splits per window, then a new window
- `--layout=tabs`: one window with a tab per session
- `--layout=saved`: sessions from a snapshot go back to their saved window and tab, and sessions that shared a tab become splits; any others get a window each

Ghostty does not report which tty a terminal is attached to, so `--save` matches each session to the Ghostty terminal whose working directory is the session's cwd. With several sessions in one folder, `--layout=saved` can put them back in a different order.

Ghostty windows opened while the screen was locked did not start their shells in testing, so run `--execute` from an unlocked session.

### Terminal.app placement (macOS)

With `--target=terminal`, each session gets its own Terminal.app window, starts in its working directory and resumes in your interactive shell. Terminal.app reports each tab's tty, so `--save` records the exact window and tab. Terminal.app has no scripting command for new tabs, so restore always opens windows.

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
~/.claude/sessions/<pid>.json # Claude Code's record of each running process (read-only)
~/.codex/sessions/            # Codex session storage (read-only)
~/.ai/sessions.jsonl          # Summary cache (written by restore)
~/.ai/snapshots/              # Saved snapshots from --save
~/.ai/snapshots/latest.json   # Symlink to most recent snapshot
~/bin/restore                 # Symlink to restore_terminals.py (or the directory given to install.sh)
~/bin/find-session            # Symlink to find_session.py
```

## Running the tests

```bash
python3 -m unittest discover tests
```

The tests use only the standard library. They cover process discovery, Claude session lookup, host-terminal detection, macOS window matching, the Ghostty layouts and target selection. One test starts a real process named `claude` and checks that `--save`'s discovery finds it, so running the suite on each OS covers `/proc` on Linux and `ps`/`lsof` on macOS. To run the Linux side from a Mac:

```bash
docker run --rm -v "$PWD":/repo:ro python:3.10-slim \
  bash -c 'cp -r /repo /tmp/r && cd /tmp/r && python3 -m unittest discover tests'
```

## License

MIT
