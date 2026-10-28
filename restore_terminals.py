#!/usr/bin/env python3
"""
restore_terminals.py — recover and relaunch AI coding sessions.

Scans Claude Code and Codex session history on disk, figures out which
sessions look like 'real terminals you were working in', and prints the
commands needed to reopen them as Terminator windows (Linux/i3), Ghostty or
Terminal.app windows (macOS), or tmux/byobu panes (either).

Deterministic core. Optional `--summarize` calls `claude -p` (CLI) to
summarize the last hour of each candidate session.

The default mode is a dry run. Add --execute to launch the selected sessions.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# termui.py sits beside the real file. Python adds that folder to sys.path on
# its own, but not under -I, -P or PYTHONSAFEPATH, so add it explicitly;
# resolve() follows the ~/.local/bin symlink.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from termui import TAGLINE, clip, paint, pick, wrap  # noqa: E402

HOME = Path.home()
IS_MACOS = sys.platform == "darwin"
CLAUDE_ROOT = HOME / ".claude" / "projects"
CLAUDE_PID_DIR = HOME / ".claude" / "sessions"   # <pid>.json, written by Claude Code per running process
CODEX_ROOT = HOME / ".codex" / "sessions"
DEFAULT_CACHE = HOME / ".ai" / "sessions.jsonl"
SNAPSHOTS_DIR = HOME / ".ai" / "snapshots"
SLUG_MAX_LEN = 25

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Session:
    tool: str                       # "claude" | "codex"
    session_id: str
    path: Path
    cwd: str | None
    first_ts: datetime | None
    last_ts: datetime | None
    message_count: int = 0
    user_message_count: int = 0
    first_user_prompt: str | None = None
    last_user_prompt: str | None = None
    last_role: str | None = None
    interrupted: bool = False       # last turn is assistant/tool — user wasn't replied to OR agent was mid-task
    closed_with_clear: bool = False # last user action was /clear — session was abandoned, not paused
    last_hour_transcript: str = ""  # populated lazily for summarization
    topic: str | None = None        # short label from AI (used as window title)
    slug: str | None = None         # short kebab-case slug from AI (used for tmux window/pane names)
    abstract: str | None = None     # 1-2 sentence summary from AI
    summary_error: str | None = None
    search_matches: list[dict] = field(default_factory=list)  # [{timestamp, role, snippet}]
    git_remote: str | None = None
    git_branch: str | None = None
    pr_number: int | None = None
    pr_title: str | None = None
    pr_url: str | None = None
    pr_state: str | None = None     # "open" | "merged" | "closed"
    pr_fetched_at: datetime | None = None
    workspace: str | None = None    # i3 workspace name (e.g. " 1 ")
    window_title: str | None = None # terminal window title at snapshot time
    terminal: str | None = None     # host terminal at snapshot time: ghostty, terminal, terminator, tmux, ...
    window_id: str | None = None    # macOS terminal window id at snapshot time
    tab_index: int | None = None    # 1-based tab within window_id at snapshot time

    @property
    def span_minutes(self) -> float:
        if not self.first_ts or not self.last_ts:
            return 0.0
        return (self.last_ts - self.first_ts).total_seconds() / 60.0

    @property
    def age_days(self) -> float:
        if not self.last_ts:
            return float("inf")
        return (datetime.now(timezone.utc) - self.last_ts).total_seconds() / 86400.0


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Handle both "...Z" and "+00:00" forms
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _truncate(s: str | None, n: int = 120) -> str:
    return clip(s, n)


def _claude_user_text(msg: dict) -> str | None:
    """Extract plain text from a Claude user message payload."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                t = p.get("text")
                if t:
                    parts.append(t)
        return "\n".join(parts) if parts else None
    return None


def parse_claude(path: Path) -> Session | None:
    """Parse a Claude Code .jsonl into a Session summary."""
    s = Session(tool="claude", session_id=path.stem, path=path, cwd=None,
                first_ts=None, last_ts=None)
    last_user_ts: datetime | None = None
    last_assistant_ts: datetime | None = None
    recent_lines: list[tuple[datetime, str, str]] = []  # (ts, role, text)
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if s.session_id is None and d.get("sessionId"):
                    s.session_id = d["sessionId"]
                if s.cwd is None and d.get("cwd"):
                    s.cwd = d["cwd"]
                ts = _parse_ts(d.get("timestamp"))
                if ts:
                    if s.first_ts is None:
                        s.first_ts = ts
                    s.last_ts = ts
                t = d.get("type")
                msg = d.get("message") if isinstance(d.get("message"), dict) else None
                role = (msg or {}).get("role") if msg else None
                if t == "user" and role == "user":
                    text = _claude_user_text(msg) or ""
                    # skip pure tool_result payloads (Claude also uses role=user for those)
                    if text.strip():
                        s.user_message_count += 1
                        if s.first_user_prompt is None:
                            s.first_user_prompt = text
                        s.last_user_prompt = text
                        if ts:
                            last_user_ts = ts
                            recent_lines.append((ts, "user", text))
                    s.message_count += 1
                    s.last_role = "user" if text.strip() else s.last_role
                elif t == "assistant" and role == "assistant":
                    s.message_count += 1
                    s.last_role = "assistant"
                    if ts:
                        last_assistant_ts = ts
                        text = _claude_user_text(msg) or ""
                        if text.strip():
                            recent_lines.append((ts, "assistant", text))
    except OSError:
        return None
    if not s.last_ts or not s.cwd:
        return None
    # interrupted: last meaningful turn is assistant (agent was mid-reply when killed)
    if last_assistant_ts and last_user_ts and last_assistant_ts > last_user_ts:
        s.interrupted = True
    # closed_with_clear: user's last action was /clear — session was deliberately abandoned
    if s.last_user_prompt and "<command-name>/clear</command-name>" in s.last_user_prompt:
        s.closed_with_clear = True
    # last-hour transcript (for optional summarization)
    if s.last_ts:
        cutoff = s.last_ts - timedelta(hours=1)
        kept = [(ts, r, t) for ts, r, t in recent_lines if ts >= cutoff]
        s.last_hour_transcript = "\n\n".join(
            f"[{r}] {_truncate(t, 800)}" for _, r, t in kept[-40:]
        )
    return s


def parse_codex(path: Path) -> Session | None:
    """Parse a Codex rollout .jsonl into a Session summary."""
    s = Session(tool="codex", session_id=path.stem, path=path, cwd=None,
                first_ts=None, last_ts=None)
    last_user_ts: datetime | None = None
    last_agent_ts: datetime | None = None
    recent_lines: list[tuple[datetime, str, str]] = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(d.get("timestamp"))
                if ts:
                    if s.first_ts is None:
                        s.first_ts = ts
                    s.last_ts = ts
                t = d.get("type")
                p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
                if t == "session_meta":
                    s.cwd = p.get("cwd") or s.cwd
                    s.session_id = p.get("id") or s.session_id
                elif t == "event_msg" and p.get("type") == "user_message":
                    text = p.get("message") or p.get("text") or ""
                    if text.strip():
                        s.user_message_count += 1
                        if s.first_user_prompt is None:
                            s.first_user_prompt = text
                        s.last_user_prompt = text
                        s.last_role = "user"
                        if ts:
                            last_user_ts = ts
                            recent_lines.append((ts, "user", text))
                    s.message_count += 1
                elif t == "event_msg" and p.get("type") == "agent_message":
                    s.last_role = "assistant"
                    s.message_count += 1
                    if ts:
                        last_agent_ts = ts
                        text = p.get("message") or ""
                        if text.strip():
                            recent_lines.append((ts, "assistant", text))
    except OSError:
        return None
    if not s.last_ts or not s.cwd:
        return None
    if last_agent_ts and last_user_ts and last_agent_ts > last_user_ts:
        s.interrupted = True
    if s.last_ts:
        cutoff = s.last_ts - timedelta(hours=1)
        kept = [(ts, r, t) for ts, r, t in recent_lines if ts >= cutoff]
        s.last_hour_transcript = "\n\n".join(
            f"[{r}] {_truncate(t, 800)}" for _, r, t in kept[-40:]
        )
    return s


# ---------------------------------------------------------------------------
# Discovery + filter
# ---------------------------------------------------------------------------


def discover(tools: set[str], only_paths: set[Path] | None = None) -> list[Session]:
    """Parse session JSONLs into Session objects.

    If `only_paths` is given, skip files not in that set — used by `--find`
    after rg has narrowed the candidate set."""
    out: list[Session] = []
    if "claude" in tools and CLAUDE_ROOT.exists():
        for proj in CLAUDE_ROOT.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                # skip sub-agent transcripts under */subagents/*
                if "subagents" in f.parts:
                    continue
                if only_paths is not None and f not in only_paths:
                    continue
                parsed = parse_claude(f)
                if parsed:
                    out.append(parsed)
    if "codex" in tools and CODEX_ROOT.exists():
        for f in CODEX_ROOT.rglob("rollout-*.jsonl"):
            if only_paths is not None and f not in only_paths:
                continue
            parsed = parse_codex(f)
            if parsed:
                out.append(parsed)
    return out


def apply_filters(
    sessions: list[Session],
    *,
    days: int,
    min_user_msgs: int,
    min_span_minutes: float,
    project_glob: str | None,
    include_cleared: bool = False,
) -> list[Session]:
    import fnmatch
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept: list[Session] = []
    for s in sessions:
        if not s.last_ts or s.last_ts < cutoff:
            continue
        if s.user_message_count < min_user_msgs:
            continue
        if s.span_minutes < min_span_minutes:
            continue
        if not s.cwd or not Path(s.cwd).exists():
            continue
        if project_glob and not fnmatch.fnmatch(s.cwd, project_glob):
            continue
        if s.closed_with_clear and not include_cleared:
            continue
        kept.append(s)
    return kept


def rank(sessions: list[Session]) -> list[Session]:
    """Sort sessions by last activity (most recent first). No dedupe — multiple
    sessions in the same cwd are legitimate (different topics, pre-worktree)."""
    return sorted(
        sessions,
        key=lambda x: x.last_ts or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Summarization (optional — calls `claude -p`)
# ---------------------------------------------------------------------------

# Files that identify "what this project is". Scanned in the session's cwd
# (and, if cwd looks like a git worktree, also the repo root — best effort).
PROJECT_CONTEXT_FILES = [
    "README.md", "README", "README.rst", "README.txt",
    "CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursorrules",
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
]
PER_FILE_BUDGET = 4096          # bytes per project-context file
PROJECT_CONTEXT_BUDGET = 16384  # total bytes of project context
TRANSCRIPT_BUDGET = 12000       # bytes of last-hour transcript


def _repo_root(cwd: Path) -> Path | None:
    """Walk up until we see a .git dir/file (worktree); stop at $HOME."""
    home = Path.home().resolve()
    p = cwd.resolve()
    while p != p.parent and str(p).startswith(str(home)):
        if (p / ".git").exists():
            return p
        p = p.parent
    return None


def gather_project_context(cwd_str: str | None) -> str:
    """Read project-identity files from cwd (and repo root if different)."""
    if not cwd_str:
        return ""
    cwd = Path(cwd_str)
    if not cwd.is_dir():
        return ""
    roots: list[Path] = [cwd]
    rr = _repo_root(cwd)
    if rr and rr != cwd:
        roots.append(rr)
    chunks: list[str] = []
    used = 0
    seen: set[Path] = set()
    for root in roots:
        for name in PROJECT_CONTEXT_FILES:
            f = root / name
            try:
                if not f.is_file():
                    continue
                real = f.resolve()
                if real in seen:
                    continue
                seen.add(real)
                data = f.read_text(errors="replace")[:PER_FILE_BUDGET]
            except OSError:
                continue
            header = f"==== {f.relative_to(root.anchor) if root == cwd else name} ({root}) ===="
            chunk = f"{header}\n{data.strip()}\n"
            if used + len(chunk) > PROJECT_CONTEXT_BUDGET:
                break
            chunks.append(chunk)
            used += len(chunk)
        if used >= PROJECT_CONTEXT_BUDGET:
            break
    return "\n".join(chunks)


SUMMARY_PROMPT = """\
You classify a coding-agent session. Produce a concrete, scannable summary —
the goal is for someone scanning a list of 30 sessions to immediately know
which one this is.

Given:
  1. PROJECT CONTEXT — identity docs from the working directory
  2. GIT CONTEXT — branch and remote (often encodes a ticket ID or feature)
  3. LAST-HOUR TRANSCRIPT — the tail of the session right before it ended

Produce a STRICT JSON object, and nothing else:

  {{"topic": "<concrete short label, 3-8 words, Title Case, no trailing punctuation>",
    "slug": "<short kebab-case identifier, max 25 chars, lowercase letters/digits/hyphens only>",
    "abstract": "<2 sentences, <=50 words. Sentence 1: what was being worked on — name the concrete subject (component, file, feature, ticket ID). Sentence 2: the immediate next step or where the session was left off>"}}

The topic MUST name the subject, not just the verb.
  Bad: "Session Exit", "Bug Fix", "Code Review", "Documentation Pass"
  Good: "Drop Cadence Iframe Config", "Fix S3 Multipart Retry", "ISO 25010 Doc Pass"
If the branch encodes a ticket (e.g. PROJ-1234), include it. If the subject is
genuinely unclear after reading both project context and transcript, prefer
"Misc <repo-name> Cleanup" over a generic verb phrase.

No preface. No markdown fences. No trailing text. Just the JSON object.

==== PROJECT CONTEXT (cwd: {cwd}) ====
{project_ctx}

==== GIT CONTEXT ====
branch: {branch}
remote: {remote}

==== LAST-HOUR TRANSCRIPT (tool: {tool}) ====
{transcript}
"""


def _extract_json(text: str) -> dict | None:
    """Best-effort JSON object extraction from LLM output."""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def summarize(session: Session, timeout: int = 90) -> None:
    """Populate session.topic / session.abstract via `claude -p`. Mutates in place."""
    if not session.last_hour_transcript.strip():
        session.summary_error = "no last-hour transcript"
        return
    project_ctx = gather_project_context(session.cwd) or "(no README/CLAUDE.md/etc. found)"
    prompt = SUMMARY_PROMPT.format(
        cwd=session.cwd or "?",
        tool=session.tool,
        branch=session.git_branch or "(none)",
        remote=session.git_remote or "(none)",
        project_ctx=project_ctx[:PROJECT_CONTEXT_BUDGET],
        transcript=session.last_hour_transcript[:TRANSCRIPT_BUDGET],
    )
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        session.summary_error = "claude CLI not found"
        return
    except subprocess.TimeoutExpired:
        session.summary_error = "timed out"
        return
    if result.returncode != 0:
        session.summary_error = f"claude -p exited {result.returncode}: {result.stderr.strip()[:200]}"
        return
    obj = _extract_json(result.stdout)
    if not obj:
        session.summary_error = "could not parse JSON from claude output"
        session.abstract = result.stdout.strip()[:200] or None
        return
    topic = (obj.get("topic") or "").strip().rstrip(".!?") or None
    raw_slug = (obj.get("slug") or "").strip() or None
    slug = _slugify(raw_slug) or _slugify(topic)
    abstract = (obj.get("abstract") or "").strip() or None
    session.topic = topic
    session.slug = slug
    session.abstract = abstract


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

_git_cache: dict[str, dict] = {}


def _git_info(cwd: str | None) -> dict:
    if not cwd:
        return {}
    if cwd in _git_cache:
        return _git_cache[cwd]
    info: dict = {}
    if not Path(cwd).is_dir():
        _git_cache[cwd] = info
        return info
    try:
        r = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            info["remote"] = r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    try:
        r = subprocess.run(["git", "-C", cwd, "branch", "--show-current"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            info["branch"] = r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    _git_cache[cwd] = info
    return info


def hydrate_git_info(sessions: list[Session]) -> None:
    """Populate git_branch and git_remote for each session, in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    def fill(s: Session) -> None:
        if not s.cwd:
            return
        gi = _git_info(s.cwd)
        s.git_branch = gi.get("branch") or s.git_branch
        s.git_remote = gi.get("remote") or s.git_remote
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(fill, sessions))


def fetch_pr_info(session: Session, timeout: int = 10) -> None:
    """Populate session.pr_* via `gh pr list`. Mutates in place. Silent on
    failure (no gh, no auth, no remote, no PR for this branch — all map to
    'no PR info' which is fine)."""
    if not session.git_branch:
        return
    if not session.cwd or not Path(session.cwd).is_dir():
        return
    if session.git_remote and "github" not in session.git_remote:
        return
    try:
        r = subprocess.run(
            ["gh", "pr", "list",
             "--head", session.git_branch,
             "--state", "all",
             "--json", "number,title,url,state",
             "--limit", "1"],
            cwd=session.cwd,
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    if r.returncode != 0:
        # don't mark fetched_at — let it retry next run (likely auth/remote issue)
        return
    session.pr_fetched_at = datetime.now(timezone.utc)
    try:
        prs = json.loads(r.stdout)
    except json.JSONDecodeError:
        return
    if not isinstance(prs, list) or not prs:
        # successful query, no PR for this branch — clear any stale cached data
        session.pr_number = None
        session.pr_title = None
        session.pr_url = None
        session.pr_state = None
        return
    pr = prs[0]
    session.pr_number = pr.get("number")
    session.pr_title = pr.get("title")
    session.pr_url = pr.get("url")
    state = pr.get("state")
    session.pr_state = state.lower() if isinstance(state, str) else None


# ---------------------------------------------------------------------------
# Content search (--find)
# ---------------------------------------------------------------------------


def _snippet_around(text: str, pattern: re.Pattern, ctx: int = 80) -> str:
    m = pattern.search(text)
    if not m:
        return _truncate(text, 160)
    start = max(0, m.start() - ctx)
    end = min(len(text), m.end() + ctx)
    snip = text[start:end]
    if start > 0:
        snip = "…" + snip
    if end < len(text):
        snip = snip + "…"
    return " ".join(snip.split())


def _extract_all_text_claude(d: dict) -> tuple[str, str, str]:
    """(timestamp, role, all_text) from a Claude JSONL line."""
    ts = d.get("timestamp") or ""
    t = d.get("type") or ""
    msg = d.get("message") if isinstance(d.get("message"), dict) else None
    role = (msg or {}).get("role") or t
    content = (msg or {}).get("content") if msg else None
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                for key in ("text", "content", "output", "input"):
                    v = p.get(key)
                    if isinstance(v, str):
                        parts.append(v)
        text = "\n".join(parts)
    return ts, role, text


def _extract_all_text_codex(d: dict) -> tuple[str, str, str]:
    """(timestamp, role, all_text) from a Codex JSONL line."""
    ts = d.get("timestamp") or ""
    t = d.get("type") or ""
    p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
    subt = p.get("type") or "" if isinstance(p, dict) else ""
    role = subt or t
    text = ""
    for key in ("message", "text", "content", "output", "command"):
        v = p.get(key) if isinstance(p, dict) else None
        if isinstance(v, str) and v.strip():
            text += v + "\n"
    content = p.get("content") if isinstance(p, dict) else None
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                for key in ("text", "content"):
                    v = c.get(key)
                    if isinstance(v, str):
                        text += v + "\n"
    return ts, role, text


def _rg_candidate_paths(pattern_str: str, tools: set[str]) -> set[Path] | None:
    """Pre-filter session files with ripgrep. Returns the set of JSONL paths
    that contain at least one line matching `pattern_str`, or None if rg
    isn't available or rejects the pattern (caller should fall back to a
    full scan). Searches Claude and Codex roots as configured by `tools`."""
    from shutil import which
    if not which("rg"):
        return None
    paths: set[Path] = set()
    targets: list[tuple[Path, list[str]]] = []
    if "claude" in tools and CLAUDE_ROOT.exists():
        targets.append((CLAUDE_ROOT,
                        ["--glob", "*.jsonl", "--glob", "!**/subagents/**"]))
    if "codex" in tools and CODEX_ROOT.exists():
        targets.append((CODEX_ROOT, ["--glob", "rollout-*.jsonl"]))
    for root, globs in targets:
        try:
            r = subprocess.run(
                ["rg", "--files-with-matches", "--ignore-case",
                 *globs, "-e", pattern_str, str(root)],
                capture_output=True, text=True, timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        # rg exit codes: 0 = matches, 1 = no matches, 2 = error (bad regex etc.)
        if r.returncode == 2:
            return None
        if r.returncode not in (0, 1):
            return None
        for line in r.stdout.splitlines():
            line = line.strip()
            if line:
                paths.add(Path(line))
    return paths


def search_session(session: Session, pattern: re.Pattern,
                   max_matches: int = 10) -> bool:
    """Search a session's JSONL for pattern. Populates session.search_matches
    and git info. Returns True if any match found."""
    extractor = (_extract_all_text_claude if session.tool == "claude"
                 else _extract_all_text_codex)
    matches: list[dict] = []
    try:
        with session.path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # fast reject: raw line doesn't contain pattern → skip parse
                if not pattern.search(line):
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts, role, text = extractor(d)
                if text and pattern.search(text) and len(matches) < max_matches:
                    matches.append({
                        "timestamp": ts,
                        "role": role,
                        "snippet": _snippet_around(text, pattern),
                    })
    except OSError:
        return False
    # also check cwd and git metadata
    gi = _git_info(session.cwd) if session.cwd else {}
    session.git_remote = gi.get("remote")
    session.git_branch = gi.get("branch")
    cwd_match = bool(session.cwd and pattern.search(session.cwd))
    remote_match = bool(session.git_remote and pattern.search(session.git_remote))
    branch_match = bool(session.git_branch and pattern.search(session.git_branch))
    if not matches and not cwd_match and not remote_match and not branch_match:
        return False
    session.search_matches = matches
    return True


def search_relevance(s: Session) -> float:
    """Higher = more relevant. Factors: match count, recency, user-msg ratio."""
    score = len(s.search_matches) * 10.0
    # weight user/assistant matches higher than tool output
    for m in s.search_matches:
        if m["role"] in ("user", "user_message"):
            score += 5.0
        elif m["role"] in ("assistant", "agent_message"):
            score += 2.0
    # recency bonus: sessions from today score +20, 7d ago → +0
    if s.last_ts:
        days = s.age_days
        score += max(0.0, 20.0 - days * (20.0 / 7.0))
    # branch/remote exact match bonus
    if s.git_branch and s.search_matches:
        score += 15.0
    return score


# ---------------------------------------------------------------------------
# Terminator command rendering
# ---------------------------------------------------------------------------


def resume_cmd(s: Session) -> str:
    if s.tool == "claude":
        return f"claude --resume {s.session_id}"
    # codex: resume accepts the rollout path
    return f"codex resume {shlex.quote(str(s.path))}"


def terminator_argv(s: Session, title: str) -> list[str]:
    """The exact argv that would open a new Terminator window for this session."""
    inner = f"{resume_cmd(s)}; exec $SHELL"
    return [
        "terminator",
        "-u",                                # --no-dbus: own process, own window (i3-friendly)
        "--working-directory", s.cwd or str(HOME),
        "--title", title,
        "-x", "bash", "-lc", inner,
    ]


def launch(sessions: list[Session]) -> int:
    """Spawn one detached Terminator window per session. Returns count launched."""
    import time
    launched = 0
    procs: list[tuple[Session, subprocess.Popen, str]] = []  # (session, proc, title)
    for s in sessions:
        title = _truncate(s.topic or s.first_user_prompt or "(empty)", 60)
        argv = terminator_argv(s, title)
        try:
            p = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            ws_label = f" → ws={s.workspace!r}" if s.workspace and s.workspace != "?" else ""
            procs.append((s, p, title))
            launched += 1
            print(f"  ✓ spawned pid={p.pid}: {short_cwd(s.cwd)}  —  {title}{ws_label}")
        except FileNotFoundError:
            print(f"  ✗ terminator not found in PATH — aborting", file=sys.stderr)
            return launched
        except OSError as e:
            print(f"  ✗ failed for {short_cwd(s.cwd)}: {e}", file=sys.stderr)
        time.sleep(0.2)
    # wait for windows to appear, then move to saved workspaces
    time.sleep(1.0)
    for s, p, title in procs:
        rc = p.poll()
        if rc is not None and rc != 0:
            print(f"  [!] pid={p.pid} exited rc={rc} ({short_cwd(s.cwd)}) — "
                  f"see Terminator output above.", file=sys.stderr)
            continue
        if s.workspace and s.workspace != "?":
            # find the window by PID via wmctrl and move it
            try:
                wm = subprocess.run(["wmctrl", "-lp"], capture_output=True, text=True, timeout=3)
                for line in wm.stdout.strip().splitlines():
                    parts = line.split(None, 4)
                    if len(parts) >= 3 and int(parts[2]) == p.pid:
                        wid = parts[0]
                        subprocess.run(
                            ["i3-msg", f'[id={wid}] move to workspace {s.workspace}'],
                            capture_output=True, timeout=3,
                        )
                        print(f"    → moved to workspace {s.workspace!r}")
                        break
            except (subprocess.TimeoutExpired, ValueError):
                pass
    return launched


# ---------------------------------------------------------------------------
# Byobu / tmux backend
# ---------------------------------------------------------------------------


def _have(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


def _pane_title(s: Session) -> str:
    return s.slug or _slugify(s.topic) or s.session_id[:8]


def _window_name(group: list[Session], idx: int) -> str:
    first = group[0]
    base = first.slug or _slugify(first.topic) or f"win-{idx + 1}"
    if len(group) > 1:
        return f"{base}+{len(group) - 1}"
    return base


def _byobu_groups(sessions: list[Session], layout: str, max_splits: int) -> list[list[Session]]:
    if layout == "windows":
        per = 1
    elif layout == "splits":
        per = max(1, len(sessions))
    else:  # hybrid
        per = max(1, max_splits)
    return [sessions[i:i + per] for i in range(0, len(sessions), per)]


def launch_byobu(
    sessions: list[Session],
    *,
    layout: str,
    max_splits: int,
    split_direction: str,
    session_name: str,
) -> int:
    """Build a tmux/byobu session with the picked sessions as panes/windows.

    Returns the count of panes successfully placed. Does not attach — prints
    the attach command at the end (or a switch-client hint if already in tmux).
    """
    if not _have("tmux"):
        print("  ✗ tmux not found in PATH (install byobu / tmux first)", file=sys.stderr)
        return 0
    if not sessions:
        return 0

    groups = _byobu_groups(sessions, layout, max_splits)
    split_flag = "-h" if split_direction == "h" else "-v"

    has_session = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    ).returncode == 0

    def inner(s: Session) -> str:
        return f"{resume_cmd(s)}; exec $SHELL"

    placed = 0
    for gi, group in enumerate(groups):
        wname = _window_name(group, gi)
        first = group[0]
        first_inner = inner(first)
        if gi == 0 and not has_session:
            argv = ["tmux", "new-session", "-d",
                    "-s", session_name,
                    "-n", wname,
                    "-c", first.cwd or str(HOME),
                    first_inner]
        else:
            argv = ["tmux", "new-window",
                    "-t", f"{session_name}:",
                    "-n", wname,
                    "-c", first.cwd or str(HOME),
                    first_inner]
        rc = subprocess.run(argv, capture_output=True, text=True)
        if rc.returncode != 0:
            print(f"  ✗ tmux failed creating window {wname!r}: "
                  f"{rc.stderr.strip()}", file=sys.stderr)
            return placed
        subprocess.run(
            ["tmux", "select-pane", "-t", f"{session_name}:{wname}.0",
             "-T", _pane_title(first)],
            capture_output=True,
        )
        placed += 1
        print(f"  ✓ {wname}.0  {short_cwd(first.cwd)}  —  {_pane_title(first)}")

        for s in group[1:]:
            argv = ["tmux", "split-window", split_flag,
                    "-t", f"{session_name}:{wname}",
                    "-c", s.cwd or str(HOME),
                    inner(s)]
            rc = subprocess.run(argv, capture_output=True, text=True)
            if rc.returncode != 0:
                print(f"  ✗ split failed in {wname!r}: {rc.stderr.strip()}",
                      file=sys.stderr)
                continue
            subprocess.run(
                ["tmux", "select-pane", "-t", f"{session_name}:{wname}",
                 "-T", _pane_title(s)],
                capture_output=True,
            )
            placed += 1
            print(f"  ✓ {wname}.+  {short_cwd(s.cwd)}  —  {_pane_title(s)}")

        if len(group) > 1:
            if len(group) > 4:
                tmux_layout = "tiled"
            else:
                tmux_layout = "even-horizontal" if split_direction == "h" else "even-vertical"
            subprocess.run(
                ["tmux", "select-layout", "-t", f"{session_name}:{wname}", tmux_layout],
                capture_output=True,
            )

    # show pane titles in the border so slugs are visible
    subprocess.run(
        ["tmux", "set-option", "-t", session_name, "pane-border-status", "top"],
        capture_output=True,
    )
    subprocess.run(
        ["tmux", "set-option", "-t", session_name, "pane-border-format", " #T "],
        capture_output=True,
    )

    in_tmux = bool(os.environ.get("TMUX"))
    print()
    if in_tmux:
        print(f"  built tmux session {session_name!r}. you're already in tmux — "
              f"switch with:  tmux switch-client -t {session_name}")
    else:
        print(f"  built tmux session {session_name!r}. attach with:  "
              f"byobu attach -t {session_name}   (or: tmux attach -t {session_name})")
    return placed


# ---------------------------------------------------------------------------
# macOS backends: Ghostty and Terminal.app, driven through AppleScript
# ---------------------------------------------------------------------------


def _resolve_target(target: str, sessions: list[Session]) -> str:
    """Pick a backend for --target=auto.

    Linux keeps Terminator. On macOS, sessions from a snapshot go back to the
    terminal they were saved from; otherwise the terminal running restore.
    """
    if target != "auto":
        return target
    if not IS_MACOS:
        return "terminator"
    saved = {s.terminal for s in sessions} & {"ghostty", "terminal"}
    if len(saved) == 1:
        return saved.pop()
    current = _current_terminal()
    if current in ("ghostty", "terminal"):
        return current
    if current is None:
        # no terminal among the parents (e.g. restore run from a script): trust the environment
        if os.environ.get("TERM_PROGRAM") == "ghostty":
            return "ghostty"
    else:
        print(f"  restore cannot open windows in {current}; using Terminal.app "
              f"(choose with --target)", file=sys.stderr)
    return "terminal"


def _current_terminal() -> str | None:
    """The terminal restore runs in, from its parent processes. TERM_PROGRAM
    is not enough: tmux overwrites it. Inside tmux, the walk starts from the
    tmux client attached to this session, which runs in the real terminal."""
    procs = _list_procs()
    host = _host_terminal(os.getpid(), procs)
    if host == "tmux" and os.environ.get("TMUX"):
        try:
            out = subprocess.run(["tmux", "display-message", "-p", "#{client_pid}"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            out = ""
        if out.isdigit():
            host = _host_terminal(int(out), procs) or host
    return host


def _shell_quote_path(path: str) -> str:
    """Quote a path for the shell, writing a folder under HOME as ~/... ; the
    tilde stays outside the quotes so the shell still expands it."""
    home = str(HOME)
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~/" + shlex.quote(path[len(home) + 1:])
    return shlex.quote(path)


def _shell_line(s: Session) -> str:
    """What gets typed into a fresh interactive shell to resume a session."""
    return f"cd -- {_shell_quote_path(s.cwd or str(HOME))} && {resume_cmd(s)}"


def ghostty_placements(sessions: list[Session], layout: str,
                       max_splits: int) -> list[tuple[Session, str]]:
    """Order sessions and give each a Ghostty placement: window, tab or split.

    windows, splits and hybrid group sessions the way the tmux backend does,
    with a Ghostty window in place of a tmux window, so no tabs are made.
    tabs puts every session in one window. saved sends sessions saved from
    Ghostty back to their saved window and tab (sessions that shared a tab
    become splits) and gives the rest a window each.
    """
    if layout == "tabs":
        return [(s, "tab" if i else "window") for i, s in enumerate(sessions)]
    if layout != "saved":
        return [(s, "split" if si else "window")
                for group in _byobu_groups(sessions, layout, max_splits)
                for si, s in enumerate(group)]

    saved = [s for s in sessions if s.terminal == "ghostty" and s.window_id]
    rest = [s for s in sessions if not (s.terminal == "ghostty" and s.window_id)]

    window_order: dict[str, int] = {}
    for s in saved:
        window_order.setdefault(s.window_id, len(window_order))
    saved.sort(key=lambda s: (window_order[s.window_id], s.tab_index or 0))

    out: list[tuple[Session, str]] = []
    prev: tuple[str, int | None] | None = None
    for s in saved:
        if prev is None or prev[0] != s.window_id:
            mode = "window"
        elif prev[1] != s.tab_index:
            mode = "tab"
        else:
            mode = "split"
        out.append((s, mode))
        prev = (s.window_id, s.tab_index)
    return out + [(s, "window") for s in rest]


_GHOSTTY_LAUNCH = """
on run argv
  tell application "Ghostty"
    activate
    set w to missing value
    set t to missing value
    set n to 0
    try
      repeat with i from 1 to (count of argv) by 3
        set mode to item i of argv
        set cfg to new surface configuration
        set initial working directory of cfg to item (i + 1) of argv
        set initial input of cfg to (item (i + 2) of argv) & linefeed
        if mode is "window" or w is missing value then
          set w to new window with configuration cfg
          set t to selected tab of w
        else if mode is "tab" then
          set t to new tab in w with configuration cfg
        else
          split (focused terminal of t) direction right with configuration cfg
        end if
        set n to n + 1
      end repeat
    on error errMsg
      return (n as text) & "|" & errMsg
    end try
    return n as text
  end tell
end run
"""

_TERMINAL_LAUNCH = """
on run argv
  set wasRunning to application "Terminal" is running
  tell application "Terminal"
    set n to 0
    try
      repeat with i from 1 to (count of argv) by 2
        if n is 0 and not wasRunning then
          -- a cold start opens a default window; use it instead of leaving it empty
          delay 1
          if (count of windows) > 0 then
            set t to do script (item i of argv) in window 1
          else
            set t to do script (item i of argv)
          end if
        else
          set t to do script (item i of argv)
        end if
        set custom title of t to item (i + 1) of argv
        set n to n + 1
      end repeat
    on error errMsg
      activate
      return (n as text) & "|" & errMsg
    end try
    activate
    return n as text
  end tell
end run
"""


def _osascript_progress(script: str, argv: list[str]) -> tuple[int, str | None]:
    """Run a launch script. It returns how many items it opened, plus "|" and
    the error when one stopped it part-way."""
    out = _osascript(script, *argv, timeout=60)
    if out is None:
        return 0, "osascript failed"
    count, _, err = out.partition("|")
    try:
        return int(count or 0), err or None
    except ValueError:
        return 0, out


def _report_launch(app: str, lines: list[str], opened: int, err: str | None, hint: str) -> None:
    """One line per session, after the fact: ✓ for what opened, ✗ for the rest."""
    for i, line in enumerate(lines):
        print(f"  {'✓' if i < opened else '✗'} {line}")
    if opened < len(lines):
        why = f": {err}" if err else ""
        print(f"  ✗ {app} opened {opened}/{len(lines)}{why}. {hint}", file=sys.stderr)


def launch_ghostty(sessions: list[Session], *, layout: str, max_splits: int) -> int:
    """Open the sessions in Ghostty windows, tabs and splits. Needs Ghostty 1.3+."""
    argv: list[str] = []
    lines: list[str] = []
    for s, mode in ghostty_placements(sessions, layout, max_splits):
        argv += [mode, s.cwd or str(HOME), _shell_line(s)]
        lines.append(f"{mode:<6} {short_cwd(s.cwd)}  —  {_pane_title(s)}")
    n, err = _osascript_progress(_GHOSTTY_LAUNCH, argv)
    _report_launch("Ghostty", lines, n, err,
                   "It needs Ghostty 1.3+ and Automation permission for this terminal "
                   "(System Settings → Privacy & Security → Automation).")
    return n


def launch_terminal_app(sessions: list[Session]) -> int:
    """Open one Terminal.app window per session."""
    argv: list[str] = []
    lines: list[str] = []
    for s in sessions:
        title = _truncate(s.topic or s.first_user_prompt or "(empty)", 60)
        argv += [_shell_line(s), title]
        lines.append(f"window {short_cwd(s.cwd)}  —  {title}")
    n, err = _osascript_progress(_TERMINAL_LAUNCH, argv)
    _report_launch("Terminal", lines, n, err,
                   "Check Automation permission "
                   "(System Settings → Privacy & Security → Automation).")
    return n


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "?"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def short_cwd(cwd: str | None) -> str:
    if not cwd:
        return "?"
    home = str(HOME)
    if cwd.startswith(home):
        return "~" + cwd[len(home):]
    return cwd


def _slugify(text: str | None, max_len: int = SLUG_MAX_LEN) -> str | None:
    if not text:
        return None
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not s:
        return None
    return s[:max_len].rstrip("-") or None


def print_plan(sessions: list[Session], use_summary: bool, target: str = "terminator",
               layout: str = "hybrid", max_splits: int = 4) -> None:
    if not sessions:
        print("No sessions matched the filters. Try --days 30 or lower --min-messages.")
        return

    placement: dict[int, str] = {}
    if target == "byobu":
        head = f"\n  Would place {len(sessions)} pane(s) into a byobu/tmux session  (--dry-run):\n"
    elif target == "ghostty":
        placed = ghostty_placements(sessions, layout, max_splits)
        sessions = [s for s, _ in placed]
        placement = {id(s): mode for s, mode in placed}
        head = f"\n  Would open {len(sessions)} Ghostty terminal(s)  (--dry-run):\n"
    elif target == "terminal":
        head = f"\n  Would open {len(sessions)} Terminal.app window(s)  (--dry-run):\n"
    else:
        head = (f"\n  Would open {len(sessions)} Terminator window(s)  "
                f"(--dry-run; i3 will tile them):\n")
    print(head)
    for i, s in enumerate(sessions, 1):
        flag = " [INTERRUPTED]" if s.interrupted else ""
        title = _truncate(s.topic or s.first_user_prompt or "(empty)", 60)
        print(f"  ── window {i}/{len(sessions)}{flag} ──")
        print(f"    tool    : {s.tool}")
        print(f"    cwd     : {short_cwd(s.cwd)}")
        print(f"    session : {s.session_id}")
        print(f"    last act: {fmt_dt(s.last_ts)}  ({s.age_days:.1f}d ago)")
        print(f"    msgs    : {s.message_count} total · {s.user_message_count} user · "
              f"{s.span_minutes:.0f}min span")
        if s.git_remote or s.git_branch:
            git_parts = []
            if s.git_remote:
                git_parts.append(f"remote={s.git_remote}")
            if s.git_branch:
                git_parts.append(f"branch={s.git_branch}")
            print(f"    git     : {'  '.join(git_parts)}")
        if s.pr_number:
            pr_line = f"#{s.pr_number}"
            if s.pr_state:
                pr_line += f" ({s.pr_state})"
            if s.pr_url:
                pr_line += f"  {s.pr_url}"
            print(f"    pr      : {pr_line}")
            if s.pr_title:
                print(f"              {_truncate(s.pr_title, 100)}")
        if use_summary:
            if s.topic:
                print(f"    topic   : {s.topic}")
            if s.abstract:
                print(f"    abstract: {_truncate(s.abstract, 220)}")
            if s.summary_error:
                print(f"    [!]     : summary unavailable — {s.summary_error}")
        if not (use_summary and s.topic):
            print(f"    first   : {_truncate(s.first_user_prompt, 100)}")
            print(f"    last    : {_truncate(s.last_user_prompt, 100)}")
        if s.search_matches:
            print(f"    hits    : {len(s.search_matches)} match(es)")
            for j, m in enumerate(s.search_matches[:3]):
                ts_short = m["timestamp"][:16] if m["timestamp"] else ""
                print(f"      [{j+1}] {ts_short} [{m['role']}] "
                      f"{_truncate(m['snippet'], 140)}")
        if s.window_id:
            print(f"    saved in: {s.terminal} window {s.window_id}, tab {s.tab_index}")
        print(f"    title   : {title}")
        if target == "byobu":
            print(f"    slug    : {_pane_title(s)}")
            print(f"    cmd     : {resume_cmd(s)}")
        elif target == "ghostty":
            print(f"    opens as: {placement[id(s)]}")
            print(f"    cmd     : {_shell_line(s)}")
        elif target == "terminal":
            print(f"    cmd     : {_shell_line(s)}")
        else:
            argv = terminator_argv(s, title)
            print(f"    cmd     : {' '.join(shlex.quote(a) for a in argv)}")
        print()


def print_stats(all_found: list[Session], after_filter: list[Session],
                ranked: list[Session]) -> None:
    arrow = paint("→", "accent")
    print(f"  {paint('scanned:', 'accent')} {paint(len(all_found), 'bold')} sessions on disk  "
          f"{arrow} {paint(len(after_filter), 'bold')} after filters  "
          f"{arrow} {paint(len(ranked), 'ok')} ranked by last activity")


# ---------------------------------------------------------------------------
# Live snapshot: detect running sessions
# ---------------------------------------------------------------------------


def _encode_cwd_to_project(cwd: str) -> str:
    """Encode a cwd the way Claude Code names project dirs: /a/b.c → -a-b-c"""
    import re
    return re.sub(r'[^a-zA-Z0-9]', '-', cwd)


def _most_recent_jsonl(project_dir: Path) -> tuple[str, Path] | None:
    """Return (session_id, path) of the most recently modified JSONL in a project dir."""
    best: tuple[float, str, Path] | None = None
    for f in project_dir.glob("*.jsonl"):
        if "subagents" in f.parts:
            continue
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        if best is None or mt > best[0]:
            best = (mt, f.stem, f)
    return (best[1], best[2]) if best else None


def _claude_pid_record(pid: int) -> dict | None:
    """Claude Code's own record for a running process: ~/.claude/sessions/<pid>.json.

    It holds the live sessionId (which changes on /clear) and the launch cwd,
    so it beats guessing from argv or the newest JSONL in the project dir.
    """
    try:
        rec = json.loads((CLAUDE_PID_DIR / f"{pid}.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("pid") != pid or not rec.get("sessionId"):
        return None
    return rec


def _claude_jsonl_path(session_id: str, cwd: str | None) -> Path | None:
    if cwd:
        p = CLAUDE_ROOT / _encode_cwd_to_project(cwd) / f"{session_id}.jsonl"
        if p.exists():
            return p
    for p in CLAUDE_ROOT.glob(f"*/{session_id}.jsonl"):
        return p
    return None


def _find_claude_session(pid: int, argv: list[str], cwd: str) -> str | None:
    """Find session_id for a running Claude process."""
    # 1. Claude Code's per-pid record, when this version writes one
    rec = _claude_pid_record(pid)
    if rec:
        return rec["sessionId"]
    # 2. If --resume <id> is in argv, take it directly
    for i, arg in enumerate(argv):
        if arg == "--resume" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--resume="):
            return arg.split("=", 1)[1]
    # 3. If --continue, it's the most recent session in project dir
    # 4. Fallback: map cwd → project dir → most recent JSONL
    encoded = _encode_cwd_to_project(cwd)
    project_dir = CLAUDE_ROOT / encoded
    if project_dir.is_dir():
        result = _most_recent_jsonl(project_dir)
        if result:
            return result[0]
    return None


# ---------------------------------------------------------------------------
# Process table: /proc on Linux, ps + lsof elsewhere (macOS)
# ---------------------------------------------------------------------------


@dataclass
class Proc:
    pid: int
    ppid: int
    uid: int
    tty: str | None     # "ttys003" on macOS; None when there is no controlling tty
    argv: list[str]
    cmd: str            # the raw command line, for matching app bundle paths


def _list_procs() -> dict[int, Proc]:
    if Path("/proc").is_dir():
        return _list_procs_linux()
    try:
        out = subprocess.run(
            ["ps", "-axww", "-o", "pid=,ppid=,uid=,tty=,args="],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    return _parse_ps(out)


def _parse_ps(out: str) -> dict[int, Proc]:
    """Parse `ps -o pid=,ppid=,uid=,tty=,args=`. argv is split on whitespace,
    which is lossy for arguments with spaces but enough to spot the tool and
    a --resume id."""
    procs: dict[int, Proc] = {}
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid, ppid, uid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        tty = None if parts[3] in ("??", "-", "?") else parts[3]
        procs[pid] = Proc(pid, ppid, uid, tty, parts[4].split(), parts[4])
    return procs


def _list_procs_linux() -> dict[int, Proc]:
    procs: dict[int, Proc] = {}
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            uid = pid_dir.stat().st_uid
            raw = (pid_dir / "cmdline").read_bytes()
            # comm is in parens and may contain spaces; ppid is the 2nd field after it
            ppid = int((pid_dir / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        argv = [a for a in raw.decode("utf-8", errors="replace").split("\0") if a]
        procs[int(pid_dir.name)] = Proc(int(pid_dir.name), ppid, uid, None, argv, " ".join(argv))
    return procs


def _parse_lsof_names(out: str) -> dict[int, list[str]]:
    """Parse `lsof -F pn` output into {pid: [file names]}."""
    names: dict[int, list[str]] = {}
    pid = None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
            else:
                names.setdefault(pid, [])
        elif line.startswith("n") and pid is not None:
            names[pid].append(line[1:])
    return names


def _proc_cwds(pids: list[int]) -> dict[int, str]:
    if not pids:
        return {}
    if Path("/proc").is_dir():
        cwds = {}
        for pid in pids:
            try:
                cwds[pid] = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                pass
        return cwds
    try:
        out = subprocess.run(
            ["lsof", "-n", "-P", "-a", "-d", "cwd", "-p", ",".join(map(str, pids)), "-F", "pn"],
            capture_output=True, text=True, timeout=10,
        ).stdout  # exits 1 when any pid is gone; the rest is still printed
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    return {pid: names[0] for pid, names in _parse_lsof_names(out).items() if names}


def _proc_open_files(pid: int) -> list[str]:
    if Path("/proc").is_dir():
        paths = []
        try:
            fds = list(Path(f"/proc/{pid}/fd").iterdir())
        except OSError:
            return []
        for fd in fds:
            try:
                paths.append(os.readlink(fd))
            except OSError:
                continue
        return paths
    try:
        # -n -P: no DNS or port-name lookups for the process's sockets, which
        # can outlast the timeout when the network is slow or down
        out = subprocess.run(["lsof", "-n", "-P", "-p", str(pid), "-F", "pn"],
                             capture_output=True, text=True, timeout=10).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return _parse_lsof_names(out).get(pid, [])


def _find_codex_session(pid: int) -> tuple[str, Path] | None:
    """Find session_id for a running Codex process via its open files."""
    codex_root = str(CODEX_ROOT)
    for target in _proc_open_files(pid):
        if target.startswith(codex_root) and target.endswith(".jsonl"):
            p = Path(target)
            # session_id is in the filename: rollout-<timestamp>-<uuid>.jsonl
            return (p.stem, p)
    return None


def _host_terminal(pid: int, procs: dict[int, Proc]) -> str | None:
    """Walk up the parent chain to the terminal hosting a process."""
    check = pid
    for _ in range(12):
        p = procs.get(check)
        if not p or p.ppid in (0, check):
            return None
        p = procs.get(p.ppid)
        if not p:
            return None
        exe = os.path.basename(p.argv[0]) if p.argv else ""
        if "Ghostty.app/" in p.cmd or exe == "ghostty":
            return "ghostty"
        if "Terminal.app/" in p.cmd:
            return "terminal"
        if "iTerm.app/" in p.cmd or "iTerm2" in exe:
            return "iterm"
        if exe.startswith("tmux"):
            return "tmux"
        if exe == "terminator" or any(os.path.basename(a) == "terminator" for a in p.argv[1:2]):
            return "terminator"
        check = p.pid
    return None


def _get_i3_window_info() -> dict[int, tuple[str, str]]:
    """Return {pid: (workspace_name, window_title)} from i3 tree + wmctrl."""
    try:
        tree = json.loads(subprocess.run(
            ["i3-msg", "-t", "get_tree"], capture_output=True, text=True, timeout=5
        ).stdout)
        wmout = subprocess.run(
            ["wmctrl", "-lp"], capture_output=True, text=True, timeout=5
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return {}
    # wid → workspace name from i3 tree
    wid_to_ws: dict[int, str] = {}
    def walk(node, ws_name=None):
        if node.get("type") == "workspace":
            ws_name = node.get("name", "?")
        wid = node.get("window")
        if wid:
            wid_to_ws[wid] = ws_name or "?"
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            walk(child, ws_name)
    walk(tree)
    # pid → (ws, title) from wmctrl, resolved via i3 tree
    pid_info: dict[int, tuple[str, str]] = {}
    for line in wmout.strip().splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        wid_int = int(parts[0], 16)
        pid = int(parts[2])
        title = parts[4] if len(parts) > 4 else "?"
        ws = wid_to_ws.get(wid_int, "?")
        pid_info[pid] = (ws, title)
    return pid_info


def _find_window_for_pid(pid: int, pid_info: dict[int, tuple[str, str]],
                         procs: dict[int, Proc]) -> tuple[str, str]:
    """Walk up parent chain to find the terminal window hosting a process."""
    check = pid
    for _ in range(5):
        if check in pid_info:
            return pid_info[check]
        p = procs.get(check)
        if not p:
            break
        check = p.ppid
    return ("?", "?")


# AppleScript output is split on ASCII unit (31) and record (30) separators,
# which cannot appear in a path or a terminal title.
_US, _RS = "\x1f", "\x1e"


def _osascript(script: str, *argv: str, timeout: int = 15) -> str | None:
    sys.stdout.flush()  # keep progress lines ahead of any error below
    try:
        r = subprocess.run(["osascript", "-", *argv], input=script,
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        # usually macOS waiting on the one-time "allow to control" prompt
        print(f"  [!] osascript: no answer after {timeout}s (is an Automation "
              f"permission prompt waiting?)", file=sys.stderr)
        return None
    if r.returncode != 0:
        print(f"  [!] osascript: {r.stderr.strip()}", file=sys.stderr)
        return None
    return r.stdout.rstrip("\n")


def _split_records(out: str | None) -> list[list[str]]:
    if not out:
        return []
    return [rec.split(_US) for rec in out.split(_RS) if rec]


_GHOSTTY_LIST = f"""
set us to character id {ord(_US)}
set rs to character id {ord(_RS)}
set out to ""
tell application "Ghostty"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with x in terminals of t
        set out to out & (id of w) & us & (index of t) & us & (working directory of x) & us & (name of x) & rs
      end repeat
    end repeat
  end repeat
end tell
return out
"""

_TERMINAL_LIST = f"""
set us to character id {ord(_US)}
set rs to character id {ord(_RS)}
set out to ""
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      set out to out & (id of w) & us & (index of t) & us & (tty of t) & us & (name of w) & rs
    end repeat
  end repeat
end tell
return out
"""


def _ghostty_terminals() -> list[dict]:
    """Every Ghostty terminal surface, in window/tab order (Ghostty 1.3+)."""
    return [
        {"window_id": r[0], "tab_index": int(r[1]), "cwd": r[2], "title": r[3]}
        for r in _split_records(_osascript(_GHOSTTY_LIST)) if len(r) == 4 and r[1].isdigit()
    ]


def _terminal_app_tabs() -> list[dict]:
    """Every Terminal.app tab with its tty, in window/tab order."""
    return [
        {"window_id": r[0], "tab_index": int(r[1]), "tty": r[2], "title": r[3]}
        for r in _split_records(_osascript(_TERMINAL_LIST)) if len(r) == 4 and r[1].isdigit()
    ]


def _session_title(jsonl_path: str | None) -> str | None:
    """The title the agent shows for a session, as it sets the terminal title:
    its last custom title (from /rename), else its last AI-generated title."""
    if not jsonl_path:
        return None
    custom = ai = None
    try:
        with open(jsonl_path, errors="replace") as f:
            for line in f:
                if '"customTitle"' not in line and '"aiTitle"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "custom-title" and d.get("customTitle"):
                    custom = d["customTitle"]
                elif d.get("type") == "ai-title" and d.get("aiTitle"):
                    ai = d["aiTitle"]
    except OSError:
        return None
    return custom or ai


def _bare_title(title: str) -> str:
    """A terminal title without the status symbol the agent puts in front (✳, ◐, ⠂ ...)."""
    i = 0
    while i < len(title) and (ord(title[i]) > 127 or title[i].isspace()):
        i += 1
    return title[i:].strip()


def _agent_title(title: str) -> bool:
    """Agent sessions title their terminal with a status symbol first; a plain shell does not."""
    return bool(title) and ord(title[0]) > 127


def _same_dir(a: str | None, b: str | None) -> bool:
    """Same folder, after symlinks: lsof reports the real path, the shell may report a link."""
    return bool(a and b) and os.path.realpath(a) == os.path.realpath(b)


def _match_macos_windows(results: list[dict], procs: dict[int, Proc],
                         ghostty: list[dict], terminal: list[dict],
                         titles: dict[int, str] | None = None) -> None:
    """Fill window_id/tab_index/window_title on snapshot entries in place.

    Only a process with a controlling tty can be in a terminal window;
    background sessions have none and are left unplaced. Terminal.app tabs
    expose their tty, so that match is exact. Ghostty does not, so a session
    is matched to, in order: the terminal titled with the session's title
    (titles maps pid to it); a terminal in the session's folder whose title
    starts with an agent status symbol; any terminal in that folder. Exact
    title matches are made for every session first, so a fallback never
    takes a terminal another session's title points at.
    """
    def place(entry: dict, hit: dict) -> None:
        entry["window_id"] = hit["window_id"]
        entry["tab_index"] = hit["tab_index"]
        entry["window_title"] = hit["title"]

    by_tty = {t["tty"]: t for t in terminal}
    titles = titles or {}
    in_ghostty: list[dict] = []
    for entry in sorted(results, key=lambda e: e["pid"]):
        p = procs.get(entry["pid"])
        if not p or not p.tty:
            continue
        if entry.get("terminal") == "terminal":
            hit = by_tty.get(f"/dev/{p.tty}")
            if hit:
                place(entry, hit)
        elif entry.get("terminal") == "ghostty":
            in_ghostty.append(entry)

    unclaimed = list(ghostty)
    rest = []
    for entry in in_ghostty:
        title = titles.get(entry["pid"])
        hit = next((c for c in unclaimed if title and _bare_title(c["title"]) == title), None)
        if hit:
            unclaimed.remove(hit)
            place(entry, hit)
        else:
            rest.append(entry)
    for entry in rest:
        here = [c for c in unclaimed if _same_dir(c["cwd"], entry["cwd"])]
        hit = next((c for c in here if _agent_title(c["title"])), None) or next(iter(here), None)
        if hit:
            unclaimed.remove(hit)
            place(entry, hit)


def snapshot_active(with_windows: bool = True) -> list[dict]:
    """Find running claude/codex processes and return their session info.

    with_windows=False skips the window manager / AppleScript queries; the
    duplicate check in --load only needs session ids.
    """
    my_uid = os.getuid()
    results: dict[str, dict] = {}  # keyed by session_id to dedupe
    procs = _list_procs()

    candidates: list[tuple[Proc, str]] = []
    for p in procs.values():
        if p.uid != my_uid or not p.argv:
            continue
        exe = os.path.basename(p.argv[0])
        # identify tool
        if exe == "claude":
            tool = "claude"
        elif exe == "codex":
            tool = "codex"
        elif exe in ("node", "bash") and any("codex" in a for a in p.argv[1:3]):
            tool = "codex"
        else:
            continue
        candidates.append((p, tool))

    cwds = _proc_cwds([p.pid for p, _ in candidates])
    for p, tool in sorted(candidates, key=lambda c: c[0].pid):
        cwd = cwds.get(p.pid)
        if not cwd:
            continue
        # find session_id
        session_id: str | None = None
        jsonl_path: str | None = None
        if tool == "claude":
            session_id = _find_claude_session(p.pid, p.argv, cwd)
            if session_id:
                rec = _claude_pid_record(p.pid)
                found = _claude_jsonl_path(session_id, (rec or {}).get("cwd") or cwd)
                if found:
                    jsonl_path = str(found)
        elif tool == "codex":
            found = _find_codex_session(p.pid)
            if found:
                session_id, jp = found
                jsonl_path = str(jp)
        if not session_id:
            continue
        # dedupe: keep first seen (lowest PID tends to be the "real" one)
        if session_id not in results:
            results[session_id] = {
                "tool": tool,
                "session_id": session_id,
                "cwd": cwd,
                "pid": p.pid,
                "jsonl_path": jsonl_path,
                "terminal": _host_terminal(p.pid, procs),
                "workspace": "?",
                "window_title": "?",
            }

    entries = list(results.values())
    if not with_windows or not entries:
        return entries
    if Path("/proc").is_dir():
        # workspace + window title from i3
        pid_info = _get_i3_window_info()
        for entry in entries:
            entry["workspace"], entry["window_title"] = _find_window_for_pid(
                entry["pid"], pid_info, procs)
    elif IS_MACOS:
        hosts = {e["terminal"] for e in entries}
        titles = {e["pid"]: _session_title(e["jsonl_path"]) for e in entries
                  if e["terminal"] == "ghostty" and e["tool"] == "claude"}
        _match_macos_windows(
            entries, procs,
            ghostty=_ghostty_terminals() if "ghostty" in hosts else [],
            terminal=_terminal_app_tabs() if "terminal" in hosts else [],
            titles={pid: t for pid, t in titles.items() if t},
        )
    return entries


def _placement_label(entry: dict) -> str:
    """Where a snapshot entry was running: i3 workspace on Linux, window/tab on macOS."""
    if entry.get("window_id"):
        return f"{entry.get('terminal')} {entry['window_id'][-6:]}/tab {entry.get('tab_index')}"
    ws = entry.get("workspace")
    if ws and ws != "?":
        return f"ws={ws!r}"
    return entry.get("terminal") or "?"


def save_snapshot(entries: list[dict]) -> Path:
    """Write snapshot to ~/.ai/snapshots/<timestamp>.json, return path."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = SNAPSHOTS_DIR / f"{ts}.json"
    # also symlink "latest"
    latest = SNAPSHOTS_DIR / "latest.json"
    with path.open("w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sessions": entries,
        }, f, indent=2)
    try:
        latest.unlink(missing_ok=True)
        latest.symlink_to(path.name)
    except OSError:
        pass
    return path


def load_snapshot(path: Path | None = None) -> list[dict]:
    """Load a snapshot file. Default: latest."""
    if path is None:
        path = SNAPSHOTS_DIR / "latest.json"
    if not path.exists():
        return []
    with path.open() as f:
        data = json.load(f)
    return data.get("sessions", [])


def snapshot_to_sessions(entries: list[dict]) -> list[Session]:
    """Convert snapshot records to Session objects by parsing the JSONL files."""
    sessions: list[Session] = []
    for rec in entries:
        path_str = rec.get("jsonl_path")
        if not path_str:
            continue
        p = Path(path_str)
        if not p.exists():
            continue
        tool = rec["tool"]
        parsed = parse_claude(p) if tool == "claude" else parse_codex(p)
        if parsed:
            parsed.workspace = rec.get("workspace")
            parsed.window_title = rec.get("window_title")
            parsed.terminal = rec.get("terminal")
            parsed.window_id = rec.get("window_id")
            parsed.tab_index = rec.get("tab_index")
            sessions.append(parsed)
    return sessions


# ---------------------------------------------------------------------------
# Summary cache (~/.ai/sessions.jsonl)
# ---------------------------------------------------------------------------
#
# Cache key = (tool, session_id). We store last_ts too; if the session has
# new activity since the cached summary, we treat the entry as stale and
# regenerate (unless --refresh forces regeneration regardless).
#
# The file is JSONL: one record per line. We rewrite atomically on save so
# a partial write can't corrupt it.


def _ts_to_iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def load_cache(path: Path) -> dict[tuple[str, str], dict]:
    if not path.exists():
        return {}
    out: dict[tuple[str, str], dict] = {}
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (rec.get("tool", ""), rec.get("session_id", ""))
                if not key[0] or not key[1]:
                    continue
                # keep the newest generated_at per key (later lines win on tie)
                prev = out.get(key)
                if (prev is None
                        or (rec.get("generated_at") or "") >= (prev.get("generated_at") or "")):
                    out[key] = rec
    except OSError:
        return {}
    return out


def save_cache(path: Path, records: dict[tuple[str, str], dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        for rec in sorted(records.values(),
                          key=lambda r: r.get("generated_at") or "",
                          reverse=True):
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def hydrate_from_cache(sessions: list[Session], cache: dict[tuple[str, str], dict]) -> None:
    """Attach cached topic/abstract/PR to sessions.

    Topic/abstract are tied to last_ts (regenerated on new activity).
    PR info is independent of session activity; we hydrate it regardless and
    rely on `needs_pr_fetch` (TTL) to decide whether to refresh."""
    for s in sessions:
        rec = cache.get((s.tool, s.session_id))
        if not rec:
            continue
        cached_last = rec.get("last_ts")
        if cached_last and cached_last == _ts_to_iso(s.last_ts):
            s.topic = rec.get("topic") or s.topic
            s.slug = rec.get("slug") or s.slug or _slugify(s.topic)
            s.abstract = rec.get("abstract") or s.abstract
        # PR info hydrates regardless of last_ts
        s.pr_number = rec.get("pr_number") or s.pr_number
        s.pr_title = rec.get("pr_title") or s.pr_title
        s.pr_url = rec.get("pr_url") or s.pr_url
        s.pr_state = rec.get("pr_state") or s.pr_state
        s.pr_fetched_at = _parse_ts(rec.get("pr_fetched_at")) or s.pr_fetched_at


def needs_summary(s: Session, cache: dict[tuple[str, str], dict], refresh: bool) -> bool:
    if refresh:
        return True
    if s.topic and s.abstract:
        return False
    rec = cache.get((s.tool, s.session_id))
    if not rec:
        return True
    # stale cache (session grew since we summarized it)
    return rec.get("last_ts") != _ts_to_iso(s.last_ts)


def needs_pr_fetch(s: Session, refresh: bool, ttl_hours: float) -> bool:
    """Re-fetch PR info if never fetched, refresh forced, or fetched > TTL ago.
    Cache misses without git context are skipped (fetch_pr_info no-ops anyway)."""
    if not s.git_branch:
        return False
    if refresh:
        return True
    if s.pr_fetched_at is None:
        return True
    age_hours = (datetime.now(timezone.utc) - s.pr_fetched_at).total_seconds() / 3600
    return age_hours > ttl_hours


def cache_record(s: Session) -> dict:
    return {
        "tool": s.tool,
        "session_id": s.session_id,
        "cwd": s.cwd,
        "last_ts": _ts_to_iso(s.last_ts),
        "topic": s.topic,
        "slug": s.slug,
        "abstract": s.abstract,
        "pr_number": s.pr_number,
        "pr_title": s.pr_title,
        "pr_url": s.pr_url,
        "pr_state": s.pr_state,
        "pr_fetched_at": _ts_to_iso(s.pr_fetched_at),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Curses TUI picker
# ---------------------------------------------------------------------------


def _row_segments(s: Session, width: int) -> list[tuple[str, str]]:
    """One picker row as (text, role) pieces; roles as in termui."""
    age = f"{s.age_days:4.1f}d"
    tool = f"{s.tool:<6}"
    cwd = short_cwd(s.cwd) or "?"
    branch = _truncate(s.git_branch, 25) if s.git_branch else ""
    pr_marker = ""
    if s.pr_number:
        # short PR badge: #123 (o)pen / (m)erged / (c)losed — keeps row tight
        st = (s.pr_state or "")[:1]
        pr_marker = f" #{s.pr_number}{st}"
    label = s.topic or _truncate(s.first_user_prompt, 80) or "(no prompt)"
    n_hits = len(s.search_matches)
    hits = f" ({n_hits} hit{'' if n_hits == 1 else 's'})" if n_hits else ""
    segs = [
        ("!" if s.interrupted else " ", "warn"),
        (f" {age} ", ""),
        (tool, "accent"),
        (f" {cwd}", ""),
    ]
    if branch:
        segs.append((f"  {branch}", "ok"))
    if pr_marker:
        segs.append((pr_marker, "ok" if s.pr_state == "open" else ""))
    if hits:
        segs.append((hits, "warn"))
    used = sum(len(t) for t, _ in segs)
    remaining = max(10, width - used - 7)
    segs.append((f"  {_truncate(label, remaining)}", ""))
    return segs


def _row_text(s: Session, width: int) -> str:
    return "".join(t for t, _ in _row_segments(s, width))


# Codex names its turns after event types; show them like the others
_ROLE_NAMES = {"user_message": "user", "agent_message": "agent"}


def _detail_lines(s: Session, width: int) -> list[list[tuple[str, str]]]:
    """Every line of the details page, as (text, role) pieces like _row_segments."""
    label_w = 13
    # 3 spaces of indent, the label column, then one space before the value
    text_w = max(20, width - label_w - 5)
    out: list[list[tuple[str, str]]] = []

    def heading(title: str) -> None:
        out.append([])
        out.append([(f" {title}", "bold")])

    def field(label: str, value, role: str = "", max_lines: int = 0,
              label_role: str = "accent") -> None:
        if value in (None, ""):
            return
        for n, line in enumerate(wrap(str(value), text_w, max_lines)):
            text = (label if n == 0 else "")[:label_w]
            out.append([(f"   {text:<{label_w}} ", label_role), (line, role)])

    def para(text: str | None, role: str = "", max_lines: int = 0) -> None:
        for line in wrap(text, width - 4, max_lines):
            out.append([("   " + line, role)])

    for line in wrap(s.topic or s.first_user_prompt or "(no prompt)", width - 2, 2):
        out.append([(f" {line}", "bold")])

    heading("Session")
    field("tool", s.tool)
    field("id", s.session_id)
    if s.interrupted:
        field("status", "interrupted: the agent had the last turn", "warn")
    elif s.closed_with_clear:
        field("status", "closed with /clear")
    else:
        field("status", "you had the last turn")
    field("started", fmt_dt(s.first_ts))
    field("last active", f"{fmt_dt(s.last_ts)}  ({s.age_days:.1f}d ago)")
    field("span", f"{s.span_minutes:.0f} min")
    field("messages", f"{s.message_count} total · {s.user_message_count} yours")

    heading("Where")
    field("folder", short_cwd(s.cwd))
    field("git remote", s.git_remote)
    field("branch", s.git_branch, "ok")
    if s.pr_number:
        field("PR", f"#{s.pr_number} ({s.pr_state or '?'})  {s.pr_title or ''}",
              "ok" if s.pr_state == "open" else "")
        field("", s.pr_url)
    if s.window_id:
        field("saved in", f"{s.terminal} window {s.window_id}, tab {s.tab_index}")
    elif s.workspace and s.workspace != "?":
        field("saved in", f"i3 workspace {s.workspace!r}")
    if s.window_title and s.window_title != "?":
        field("window", s.window_title)

    if s.abstract or s.summary_error:
        heading("Summary")
        para(s.abstract)
        if s.summary_error:
            para(f"summary unavailable: {s.summary_error}", "warn")

    heading("Prompts")
    field("first", s.first_user_prompt, max_lines=6)
    if s.last_user_prompt != s.first_user_prompt:
        field("last", s.last_user_prompt, max_lines=6)

    if s.search_matches:
        heading(f"Search hits ({len(s.search_matches)})")
        for n, m in enumerate(s.search_matches, 1):
            ts = (m.get("timestamp") or "")[:16]
            role = _ROLE_NAMES.get(m.get("role", ""), m.get("role", ""))
            field(f"[{n}] {role}", f"{ts}  {m.get('snippet', '')}", max_lines=4)

    turns = [t for t in (s.last_hour_transcript or "").split("\n\n") if t.strip()]
    if turns:
        heading(f"Last hour of conversation (last {min(len(turns), 12)} of {len(turns)} turns)")
        for turn in turns[-12:]:
            m = re.match(r"\[(\w+)\] (.*)", turn, re.S)
            role, text = (m.group(1), m.group(2)) if m else ("", turn)
            # your turns in green, the agent's in the accent colour
            field(role, text, max_lines=4, label_role="ok" if role == "user" else "accent")

    heading("Resume")
    field("command", resume_cmd(s))
    field("transcript", short_cwd(str(s.path)))
    return out


def _picker_footer(cur: Session) -> tuple[list[tuple[str, str]], str]:
    """Footer for the highlighted session: location pieces, then the abstract."""
    loc: list[tuple[str, str]] = []
    if cur.cwd:
        loc.append((short_cwd(cur.cwd), "bold"))
    if cur.git_branch:
        loc.append((f"branch: {cur.git_branch}", "ok"))
    if cur.pr_number:
        pr_str = f"PR #{cur.pr_number}"
        if cur.pr_state:
            pr_str += f" ({cur.pr_state})"
        if cur.pr_title:
            pr_str += f" — {_truncate(cur.pr_title, 60)}"
        loc.append((pr_str, "ok" if cur.pr_state == "open" else "bold"))
    loc_segs: list[tuple[str, str]] = []
    for n, part in enumerate(loc):
        if n:
            loc_segs.append(("  ·  ", ""))
        loc_segs.append(part)
    if cur.search_matches:
        desc_line = cur.search_matches[0]["snippet"]
    else:
        desc_line = (cur.abstract or _truncate(cur.last_user_prompt, 200)
                     or "(no preview)")
    return loc_segs, desc_line


def tui_pick(sessions: list[Session]) -> list[Session] | None:
    """Return user-selected subset, or None if user quit. Requires a real TTY."""
    return pick(sessions, title="restore-terminals", noun="session", row=_row_segments,
                footer=_picker_footer, details=_detail_lines, tagline=TAGLINE)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def enrich_sessions(
    sessions: list[Session],
    *,
    cache: dict[tuple[str, str], dict],
    cache_path: Path,
    summarize_enabled: bool,
    fetch_pr_enabled: bool,
    refresh: bool,
    pr_ttl_hours: float,
    summary_workers: int,
) -> None:
    """Hydrate git info, run summarize/PR-fetch as needed, persist cache.
    Mutates sessions and cache in place."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if not sessions:
        return

    # Git context first — summarize uses branch in its prompt, PR fetch uses it directly.
    hydrate_git_info(sessions)

    summary_targets: list[Session] = []
    if summarize_enabled:
        summary_targets = [s for s in sessions if needs_summary(s, cache, refresh)]
        skipped = len(sessions) - len(summary_targets)
        if skipped:
            print(f"\n  {paint('cache hit:', 'ok')} {paint(skipped, 'bold')} session(s) reused from "
                  f"{short_cwd(str(cache_path))} (use --refresh to regenerate)")
        if summary_targets:
            print(f"  {paint('summarizing', 'accent')} {paint(len(summary_targets), 'bold')} "
                  f"session(s) via `claude -p` ({summary_workers} in parallel)...")
            with ThreadPoolExecutor(max_workers=summary_workers) as ex:
                futs = {ex.submit(summarize, s): s for s in summary_targets}
                done = 0
                for fut in as_completed(futs):
                    s = futs[fut]
                    done += 1
                    marker = paint("✓", "ok") if s.topic else paint("·", "warn")
                    label = s.topic or s.summary_error or "(no topic)"
                    print(f"    {paint(f'[{done}/{len(summary_targets)}]', 'accent')} {marker} "
                          f"{short_cwd(s.cwd)}  ({s.tool}) — "
                          f"{paint(_truncate(label, 70), '' if s.topic else 'warn')}")

    pr_targets: list[Session] = []
    if fetch_pr_enabled:
        pr_targets = [s for s in sessions if needs_pr_fetch(s, refresh, pr_ttl_hours)]
        if pr_targets:
            print(f"  {paint('fetching PR info', 'accent')} for {paint(len(pr_targets), 'bold')} "
                  f"session(s) via `gh`...")
            with ThreadPoolExecutor(max_workers=6) as ex:
                list(ex.map(fetch_pr_info, pr_targets))
            n_with_pr = sum(1 for s in pr_targets if s.pr_number)
            print(f"    found {paint(n_with_pr, 'ok' if n_with_pr else 'bold')} PR(s)")

    touched_ids = {id(s) for s in summary_targets} | {id(s) for s in pr_targets}
    if touched_ids:
        for s in sessions:
            if id(s) in touched_ids:
                cache[(s.tool, s.session_id)] = cache_record(s)
        save_cache(cache_path, cache)
        print(f"  {paint('cache updated:', 'ok')} {short_cwd(str(cache_path))}")


def _launch_dispatch(args, sessions: list[Session]) -> int:
    if args.target == "byobu":
        return launch_byobu(
            sessions,
            layout=args.layout,
            max_splits=args.max_splits,
            split_direction=args.split_direction,
            session_name=args.byobu_session,
        )
    if args.target == "ghostty":
        return launch_ghostty(sessions, layout=args.layout, max_splits=args.max_splits)
    if args.target == "terminal":
        return launch_terminal_app(sessions)
    return launch(sessions)


def _backend_label(target: str) -> str:
    return {
        "byobu": "byobu/tmux pane(s)",
        "ghostty": "Ghostty terminal(s)",
        "terminal": "Terminal.app window(s)",
    }.get(target, "Terminator window(s)")


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="restore",
        description="Show which Claude/Codex sessions to reopen, and reopen them in "
                    "Terminator, Ghostty, Terminal.app or tmux.",
        epilog=(
            "Typical flow:\n"
            "  restore              # preview — last 7 days, TUI-pick, cached summaries\n"
            "  restore --execute    # same, but open them (Terminator on Linux,\n"
            "                       #   the terminal you are in on macOS)\n"
            "\n"
            "Save + restore running sessions:\n"
            "  restore --save                 # snapshot all running sessions now\n"
            "  restore --load                 # restore from last snapshot\n"
            "  restore --load --execute       # restore + launch\n"
            "\n"
            "Search for sessions by content:\n"
            "  restore --find feature/branch  # find by branch name, ticket, any text\n"
            "\n"
            "Remote / SSH (byobu / tmux panes instead of GUI windows):\n"
            "  restore --target=byobu --execute            # build a 'restore' tmux session\n"
            "  restore --target=byobu --layout=splits      # all picked sessions as splits\n"
            "  restore --target=byobu --layout=windows     # one window per session\n"
            "  restore --target=byobu --max-splits=6       # 6 splits per window, then overflow\n"
            "\n"
            "macOS:\n"
            "  restore --target=ghostty --execute          # one Ghostty window per session\n"
            "  restore --target=ghostty --layout=tabs      # one Ghostty window, a tab per session\n"
            "  restore --load --layout=saved --execute     # back into the saved windows and tabs\n"
            "  restore --target=terminal --execute         # one Terminal.app window per session\n"
            "\n"
            "Other:\n"
            "  restore 14           # lookback 14 days\n"
            "  restore --refresh    # regenerate AI summaries\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("days", nargs="?", type=int, default=7,
                    help="lookback window in days (default: 7)")
    ap.add_argument("--find", metavar="PATTERN",
                    help="search sessions by content (branch, repo, ticket, any text)")
    ap.add_argument("--refresh", action="store_true",
                    help="regenerate AI summaries (otherwise uses ~/.ai/sessions.jsonl cache)")
    ap.add_argument("--no-pick", action="store_true",
                    help="don't launch the TUI picker, print everything")
    ap.add_argument("--execute", action="store_true",
                    help="actually launch (default: dry-run only)")
    ap.add_argument("--target", choices=["auto", "terminator", "byobu", "ghostty", "terminal"],
                    default="auto",
                    help="launch backend: 'terminator' (i3 windows), 'byobu' (tmux panes/windows "
                         "in one session), 'ghostty' (Ghostty 1.3+ windows, splits or tabs, macOS) or "
                         "'terminal' (Terminal.app windows). default: auto = terminator on Linux; "
                         "on macOS the terminal a snapshot was saved from, else the one you are in")
    ap.add_argument("--layout", choices=["splits", "windows", "hybrid", "tabs", "saved"],
                    default=None,
                    help="byobu and ghostty: 'splits' = all panes in one window, "
                         "'windows' = one window per session, "
                         "'hybrid' = split up to --max-splits per window, overflow to new windows. "
                         "ghostty only: 'tabs' = one window, a tab per session; "
                         "'saved' = snapshot sessions back in their saved window and tab. "
                         "default: hybrid for byobu, windows for ghostty")
    ap.add_argument("--max-splits", type=int, default=4,
                    help="hybrid layout: max panes per window before a new one starts (default: 4)")
    ap.add_argument("--split-direction", choices=["h", "v"], default="h",
                    help="byobu only: 'h' = side-by-side (default, good for wide screens), "
                         "'v' = stacked top/bottom")
    ap.add_argument("--byobu-session", default="restore",
                    help="byobu only: tmux session name to build into (default: restore)")
    ap.add_argument("--save", action="store_true",
                    help="snapshot currently running sessions to ~/.ai/snapshots/")
    ap.add_argument("--load", nargs="?", const="latest", default=None, metavar="FILE",
                    help="restore from a snapshot (default: latest)")

    # Power-user knobs. Hidden from --help to keep the surface small.
    ap.add_argument("--min-messages", type=int, default=5, help=argparse.SUPPRESS)
    ap.add_argument("--min-span-minutes", type=float, default=10.0, help=argparse.SUPPRESS)
    ap.add_argument("--only", choices=["claude", "codex", "all"], default="all",
                    help=argparse.SUPPRESS)
    ap.add_argument("--project", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--no-ai", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-gh", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--include-cleared", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--pr-ttl-hours", type=float, default=6.0, help=argparse.SUPPRESS)
    ap.add_argument("--summary-limit", type=int, default=20, help=argparse.SUPPRESS)
    ap.add_argument("--summary-workers", type=int, default=3, help=argparse.SUPPRESS)
    ap.add_argument("--cache", default=str(DEFAULT_CACHE), help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.layout in ("tabs", "saved") and args.target not in ("auto", "ghostty"):
        ap.error(f"--layout={args.layout} needs --target=ghostty")

    # Defaults: summarize unless --no-ai; pick via TUI if TTY and not --no-pick.
    args.summarize = not args.no_ai
    args.fetch_pr = (not args.no_gh) and bool(_have("gh"))
    args.pick = (not args.no_pick) and sys.stdout.isatty() and sys.stdin.isatty()

    tools = {"claude", "codex"} if args.only == "all" else {args.only}

    # --save: snapshot currently running sessions and exit
    if args.save:
        active = snapshot_active()
        if not active:
            print("  no running claude/codex sessions found.")
            return 0
        path = save_snapshot(active)
        print(f"  saved {len(active)} session(s) to {short_cwd(str(path))}")
        for entry in active:
            wt = entry.get('window_title') or '?'
            print(f"    {_placement_label(entry):<22}  {entry['tool']:<6}  "
                  f"{short_cwd(entry['cwd']):<50}  title={wt[:60]}")
        print(f"  restore with: restore --load")
        return 0

    # --load: restore from a saved snapshot
    if args.load is not None:
        snap_path = (SNAPSHOTS_DIR / "latest.json" if args.load == "latest"
                     else Path(args.load).expanduser())
        entries = load_snapshot(snap_path)
        if not entries:
            print(f"  no snapshot found at {short_cwd(str(snap_path))}")
            return 1
        print(f"  {paint('loaded', 'ok')} {paint(len(entries), 'bold')} session(s) "
              f"from {short_cwd(str(snap_path))}")
        # Skip sessions already running
        already_running = {r["session_id"] for r in snapshot_active(with_windows=False)}
        before = len(entries)
        entries = [e for e in entries if e["session_id"] not in already_running]
        skipped = before - len(entries)
        if skipped:
            print(f"  {paint('skipped', 'accent')} {paint(skipped, 'bold')} already-running session(s)")
        if not entries:
            print(f"  {paint('all sessions are already running', 'ok')} — nothing to restore.")
            return 0
        ranked = snapshot_to_sessions(entries)
        if not ranked:
            print(f"  {paint('none of the saved sessions could be parsed', 'warn')} (files gone?)")
            return 1
        # skip the normal discover/filter path — go straight to cache+summarize+pick+launch
        cache_path = Path(args.cache).expanduser()
        cache = load_cache(cache_path)
        hydrate_from_cache(ranked, cache)
        enrich_sessions(
            ranked,
            cache=cache,
            cache_path=cache_path,
            summarize_enabled=args.summarize,
            fetch_pr_enabled=args.fetch_pr,
            refresh=args.refresh,
            pr_ttl_hours=args.pr_ttl_hours,
            summary_workers=args.summary_workers,
        )
        to_show = ranked
        if args.pick:
            picked = tui_pick(ranked)
            if picked is None:
                print("  (cancelled)")
                return 0
            to_show = picked
        args.target = _resolve_target(args.target, to_show)
        args.layout = args.layout or ("windows" if args.target == "ghostty" else "hybrid")
        if args.layout in ("tabs", "saved") and args.target != "ghostty":
            ap.error(f"--layout={args.layout} needs --target=ghostty")
        print_plan(to_show, use_summary=args.summarize, target=args.target,
                   layout=args.layout, max_splits=args.max_splits)
        if args.execute and to_show:
            print(f"  launching {len(to_show)} {_backend_label(args.target)}...")
            n = _launch_dispatch(args, to_show)
            print(f"  done — {n}/{len(to_show)} placed\n")
        else:
            print("  (dry-run — add --execute to launch)\n")
        return 0

    # --find mode: content search, then rank by relevance
    if args.find:
        try:
            pat = re.compile(args.find, re.IGNORECASE)
        except re.error as e:
            print(f"Invalid regex '{args.find}': {e}", file=sys.stderr)
            return 1
        # Pre-filter with ripgrep when available: drops parsing cost from
        # "every JSONL on disk" to "only files containing the pattern". The
        # Python pass below remains authoritative (it matches on extracted
        # text, not the raw JSON line) for snippets and cwd/branch matches.
        rg_paths = _rg_candidate_paths(args.find, tools)
        if rg_paths is not None:
            print(f"  {paint('rg pre-filter:', 'accent')} {paint(len(rg_paths), 'bold')} candidate file(s)")
        # In search mode, relax the activity filters (min msgs=1, min span=0)
        # so we don't miss sessions that briefly touched the topic.
        all_found = discover(tools, only_paths=rg_paths)
        filtered = apply_filters(
            all_found, days=args.days,
            min_user_msgs=1, min_span_minutes=0.0,
            project_glob=args.project,
            include_cleared=args.include_cleared,
        )
        print(f"  {paint('searching', 'accent')} {paint(len(filtered), 'bold')} sessions (last {args.days}d) "
              f"for /{args.find}/i ...")
        hits = [s for s in filtered if search_session(s, pat)]
        hits.sort(key=search_relevance, reverse=True)
        if not hits:
            print(f"\n  {paint(f'No sessions match {args.find!r}.', 'warn')}\n")
            return 0
        print(f"  {paint(len(hits), 'ok')} session(s) match, ranked by relevance\n")
        ranked = hits
    else:
        # Normal mode: activity-based
        all_found = discover(tools)
        filtered = apply_filters(
            all_found, days=args.days,
            min_user_msgs=args.min_messages,
            min_span_minutes=args.min_span_minutes,
            project_glob=args.project,
            include_cleared=args.include_cleared,
        )
        ranked = rank(filtered)
        print_stats(all_found, filtered, ranked)

    cache_path = Path(args.cache).expanduser()
    cache = load_cache(cache_path)

    # Always hydrate from cache — cheap, keeps previews around even without --summarize.
    hydrate_from_cache(ranked, cache)

    # Only enrich the top slice that the user is likely to see/pick. Sessions
    # past the limit get cwd-only display (still better than nothing).
    enrich_sessions(
        ranked[: args.summary_limit],
        cache=cache,
        cache_path=cache_path,
        summarize_enabled=args.summarize and bool(ranked),
        fetch_pr_enabled=args.fetch_pr and bool(ranked),
        refresh=args.refresh,
        pr_ttl_hours=args.pr_ttl_hours,
        summary_workers=args.summary_workers,
    )

    # Optional: TUI pick
    to_show = ranked
    if args.pick and ranked:
        picked = tui_pick(ranked)
        if picked is None:
            print("  (cancelled)")
            return 0
        to_show = picked
        print(f"\n  {paint('selected', 'ok')} {paint(f'{len(to_show)}/{len(ranked)}', 'bold')} session(s)")

    args.target = _resolve_target(args.target, to_show)
    args.layout = args.layout or ("windows" if args.target == "ghostty" else "hybrid")
    if args.layout in ("tabs", "saved") and args.target != "ghostty":
        ap.error(f"--layout={args.layout} needs --target=ghostty")
    print_plan(to_show, use_summary=args.summarize, target=args.target,
               layout=args.layout, max_splits=args.max_splits)
    if args.execute and to_show:
        print(f"  launching {len(to_show)} {_backend_label(args.target)}...")
        n = _launch_dispatch(args, to_show)
        print(f"  done — {n}/{len(to_show)} placed\n")
    else:
        print("  (dry-run; nothing was launched — add --execute to launch)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
