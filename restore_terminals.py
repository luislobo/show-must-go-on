#!/usr/bin/env python3
"""
restore_terminals.py — dry-run prototype.

Scans Claude Code and Codex session history on disk, figures out which
sessions look like 'real terminals you were working in', and prints the
exact `terminator` invocations that would be run to reopen each one as a
separate window (one Terminator per session — i3 will place them).

Deterministic core. Optional `--summarize` calls `claude -p` (CLI) to
summarize the last hour of each candidate session.

Nothing is launched. This is a dry-run by design.
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

HOME = Path.home()
CLAUDE_ROOT = HOME / ".claude" / "projects"
CODEX_ROOT = HOME / ".codex" / "sessions"
DEFAULT_CACHE = HOME / ".ai" / "sessions.jsonl"

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
    last_hour_transcript: str = ""  # populated lazily for summarization
    topic: str | None = None        # short label from AI (used as window title)
    abstract: str | None = None     # 1-2 sentence summary from AI
    summary_error: str | None = None
    search_matches: list[dict] = field(default_factory=list)  # [{timestamp, role, snippet}]
    git_remote: str | None = None
    git_branch: str | None = None

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
    if not s:
        return ""
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


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


def discover(tools: set[str]) -> list[Session]:
    out: list[Session] = []
    if "claude" in tools and CLAUDE_ROOT.exists():
        for proj in CLAUDE_ROOT.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                # skip sub-agent transcripts under */subagents/*
                if "subagents" in f.parts:
                    continue
                parsed = parse_claude(f)
                if parsed:
                    out.append(parsed)
    if "codex" in tools and CODEX_ROOT.exists():
        for f in CODEX_ROOT.rglob("rollout-*.jsonl"):
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
You classify a coding-agent session. Given:
  1. PROJECT CONTEXT — identity docs from the working directory
  2. LAST-HOUR TRANSCRIPT — the tail of the session right before it ended

Produce a STRICT JSON object, and nothing else:

  {{"topic": "<3-8 word label, Title Case, no trailing punctuation>",
    "abstract": "<1-2 sentences, <=40 words, describing what was being worked on and where it was left off>"}}

No preface. No markdown fences. No trailing text. Just the JSON object.

==== PROJECT CONTEXT (cwd: {cwd}) ====
{project_ctx}

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
    abstract = (obj.get("abstract") or "").strip() or None
    session.topic = topic
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
    procs: list[tuple[Session, subprocess.Popen]] = []
    for s in sessions:
        title = _truncate(s.topic or s.first_user_prompt or "(empty)", 60)
        argv = terminator_argv(s, title)
        try:
            # stdin -> /dev/null; stdout/stderr inherited so Terminator errors
            # (e.g. DBUS issues, missing DISPLAY) surface in this terminal.
            p = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                start_new_session=True,   # detach — survives this script exiting
                close_fds=True,
            )
            procs.append((s, p))
            launched += 1
            print(f"  ✓ spawned pid={p.pid}: {short_cwd(s.cwd)}  —  {title}")
        except FileNotFoundError:
            print(f"  ✗ terminator not found in PATH — aborting", file=sys.stderr)
            return launched
        except OSError as e:
            print(f"  ✗ failed for {short_cwd(s.cwd)}: {e}", file=sys.stderr)
        # small stagger so i3 / Terminator dbus don't race
        time.sleep(0.2)
    # brief sanity check: any that died immediately?
    time.sleep(0.8)
    for s, p in procs:
        rc = p.poll()
        if rc is not None and rc != 0:
            print(f"  [!] pid={p.pid} exited rc={rc} ({short_cwd(s.cwd)}) — "
                  f"Terminator likely folded into an existing instance or died; "
                  f"see its output above.", file=sys.stderr)
    return launched


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


def print_plan(sessions: list[Session], use_summary: bool) -> None:
    if not sessions:
        print("No sessions matched the filters. Try --days 30 or lower --min-messages.")
        return

    print(f"\n  Would open {len(sessions)} Terminator window(s)  "
          f"(--dry-run; i3 will tile them):\n")
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
        print(f"    title   : {title}")
        argv = terminator_argv(s, title)
        print(f"    cmd     : {' '.join(shlex.quote(a) for a in argv)}")
        print()


def print_stats(all_found: list[Session], after_filter: list[Session],
                ranked: list[Session]) -> None:
    print(f"  scanned: {len(all_found)} sessions on disk  "
          f"→ {len(after_filter)} after filters  "
          f"→ {len(ranked)} ranked by last activity")


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
    """Attach cached topic/abstract to sessions whose last_ts matches the cache."""
    for s in sessions:
        rec = cache.get((s.tool, s.session_id))
        if not rec:
            continue
        cached_last = rec.get("last_ts")
        if cached_last and cached_last == _ts_to_iso(s.last_ts):
            s.topic = rec.get("topic") or s.topic
            s.abstract = rec.get("abstract") or s.abstract


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


def cache_record(s: Session) -> dict:
    return {
        "tool": s.tool,
        "session_id": s.session_id,
        "cwd": s.cwd,
        "last_ts": _ts_to_iso(s.last_ts),
        "topic": s.topic,
        "abstract": s.abstract,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Curses TUI picker
# ---------------------------------------------------------------------------


def _row_text(s: Session, width: int) -> str:
    tag = "!" if s.interrupted else " "
    age = f"{s.age_days:4.1f}d"
    tool = f"{s.tool:<6}"
    cwd = short_cwd(s.cwd) or "?"
    label = s.topic or _truncate(s.first_user_prompt, 80) or "(no prompt)"
    hits = f" ({len(s.search_matches)} hits)" if s.search_matches else ""
    left = f"{tag} {age} {tool} {cwd}{hits}"
    remaining = max(10, width - len(left) - 7)
    return f"{left}  {_truncate(label, remaining)}"


def tui_pick(sessions: list[Session]) -> list[Session] | None:
    """Return user-selected subset, or None if user quit. Requires a real TTY."""
    if not sessions:
        return []
    try:
        import curses
    except ImportError:
        print("curses not available — skipping TUI, returning all sessions.",
              file=sys.stderr)
        return list(sessions)

    selected = [True] * len(sessions)

    def run(stdscr):
        curses.curs_set(0)
        stdscr.keypad(True)
        idx = 0
        top = 0
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            body_h = max(1, h - 3)
            # scroll
            if idx < top:
                top = idx
            elif idx >= top + body_h:
                top = idx - body_h + 1
            # header
            count_sel = sum(selected)
            header = (f" restore-terminals — {count_sel}/{len(sessions)} selected   "
                      f"[space] toggle   [a] all   [n] none   [i] invert   "
                      f"[enter] confirm   [q] quit ")
            try:
                stdscr.addnstr(0, 0, header.ljust(w)[:w], w, curses.A_REVERSE)
            except curses.error:
                pass
            # rows
            for i in range(top, min(len(sessions), top + body_h)):
                s = sessions[i]
                mark = "[x]" if selected[i] else "[ ]"
                text = f"{mark} {_row_text(s, w - 5)}"
                attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
                try:
                    stdscr.addnstr(1 + (i - top), 0, text[:w], w, attr)
                except curses.error:
                    pass
            # footer: show match snippet (search mode) or abstract
            cur = sessions[idx]
            if cur.search_matches:
                footer = cur.search_matches[0]["snippet"]
            else:
                footer = (cur.abstract or _truncate(cur.last_user_prompt, 200)
                          or "(no preview)")
            try:
                stdscr.addnstr(h - 1, 0, _truncate(footer, w - 1)[:w], w,
                               curses.A_DIM)
            except curses.error:
                pass
            stdscr.refresh()
            c = stdscr.getch()
            if c in (ord("q"), 27):  # q or ESC
                return None
            if c in (curses.KEY_DOWN, ord("j")):
                idx = min(len(sessions) - 1, idx + 1)
            elif c in (curses.KEY_UP, ord("k")):
                idx = max(0, idx - 1)
            elif c == curses.KEY_NPAGE:
                idx = min(len(sessions) - 1, idx + body_h)
            elif c == curses.KEY_PPAGE:
                idx = max(0, idx - body_h)
            elif c in (curses.KEY_HOME, ord("g")):
                idx = 0
            elif c in (curses.KEY_END, ord("G")):
                idx = len(sessions) - 1
            elif c == ord(" "):
                selected[idx] = not selected[idx]
            elif c == ord("a"):
                for i in range(len(selected)):
                    selected[i] = True
            elif c == ord("n"):
                for i in range(len(selected)):
                    selected[i] = False
            elif c == ord("i"):
                for i in range(len(selected)):
                    selected[i] = not selected[i]
            elif c in (10, 13, curses.KEY_ENTER):
                return [s for s, keep in zip(sessions, selected) if keep]

    try:
        import curses
        return curses.wrapper(run)
    except Exception as e:
        print(f"TUI error: {e}", file=sys.stderr)
        return list(sessions)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="restore",
        description="Show which Claude/Codex sessions to reopen as Terminator windows.",
        epilog=(
            "Typical flow:\n"
            "  restore              # preview plan — last 7 days, TUI-pick, cached summaries\n"
            "  restore --execute    # same but actually open each Terminator window\n"
            "\n"
            "Search for sessions by content:\n"
            "  restore --find feature/my-branch           # find by branch name\n"
            "  restore --find DSBF-491                    # find by ticket\n"
            "  restore --find feature/my-branch --execute  # find + launch\n"
            "\n"
            "Other examples:\n"
            "  restore 14                     # lookback 14 days\n"
            "  restore --refresh              # regenerate AI summaries\n"
            "  restore --no-pick              # print everything, skip TUI\n"
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
                    help="actually launch each Terminator window (default: dry-run only)")

    # Power-user knobs. Hidden from --help to keep the surface small.
    ap.add_argument("--min-messages", type=int, default=5, help=argparse.SUPPRESS)
    ap.add_argument("--min-span-minutes", type=float, default=10.0, help=argparse.SUPPRESS)
    ap.add_argument("--only", choices=["claude", "codex", "all"], default="all",
                    help=argparse.SUPPRESS)
    ap.add_argument("--project", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--no-ai", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--summary-limit", type=int, default=20, help=argparse.SUPPRESS)
    ap.add_argument("--summary-workers", type=int, default=3, help=argparse.SUPPRESS)
    ap.add_argument("--cache", default=str(DEFAULT_CACHE), help=argparse.SUPPRESS)
    args = ap.parse_args()

    # Defaults: summarize unless --no-ai; pick via TUI if TTY and not --no-pick.
    args.summarize = not args.no_ai
    args.pick = (not args.no_pick) and sys.stdout.isatty() and sys.stdin.isatty()

    tools = {"claude", "codex"} if args.only == "all" else {args.only}

    # --find mode: content search, then rank by relevance
    if args.find:
        try:
            pat = re.compile(args.find, re.IGNORECASE)
        except re.error as e:
            print(f"Invalid regex '{args.find}': {e}", file=sys.stderr)
            return 1
        # In search mode, relax the activity filters (min msgs=1, min span=0)
        # so we don't miss sessions that briefly touched the topic.
        all_found = discover(tools)
        filtered = apply_filters(
            all_found, days=args.days,
            min_user_msgs=1, min_span_minutes=0.0,
            project_glob=args.project,
        )
        print(f"  searching {len(filtered)} sessions (last {args.days}d) "
              f"for /{args.find}/i ...")
        hits = [s for s in filtered if search_session(s, pat)]
        hits.sort(key=search_relevance, reverse=True)
        if not hits:
            print(f"\n  No sessions match '{args.find}'.\n")
            return 0
        print(f"  {len(hits)} session(s) match, ranked by relevance\n")
        ranked = hits
    else:
        # Normal mode: activity-based
        all_found = discover(tools)
        filtered = apply_filters(
            all_found, days=args.days,
            min_user_msgs=args.min_messages,
            min_span_minutes=args.min_span_minutes,
            project_glob=args.project,
        )
        ranked = rank(filtered)
        print_stats(all_found, filtered, ranked)

    cache_path = Path(args.cache).expanduser()
    cache = load_cache(cache_path)

    # Always hydrate from cache — cheap, keeps previews around even without --summarize.
    hydrate_from_cache(ranked, cache)

    if args.summarize and ranked:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        targets = [s for s in ranked[: args.summary_limit]
                   if needs_summary(s, cache, args.refresh)]
        skipped = min(len(ranked), args.summary_limit) - len(targets)
        if skipped:
            print(f"\n  cache hit: {skipped} session(s) reused from "
                  f"{cache_path} (use --refresh to regenerate)")
        if targets:
            print(f"  summarizing {len(targets)} session(s) via `claude -p` "
                  f"({args.summary_workers} in parallel)...")
            with ThreadPoolExecutor(max_workers=args.summary_workers) as ex:
                futs = {ex.submit(summarize, s): s for s in targets}
                done = 0
                for fut in as_completed(futs):
                    s = futs[fut]
                    done += 1
                    marker = "✓" if s.topic else "·"
                    label = s.topic or s.summary_error or "(no topic)"
                    print(f"    [{done}/{len(targets)}] {marker} {short_cwd(s.cwd)}  "
                          f"({s.tool}) — {_truncate(label, 70)}")
            # persist new/refreshed records
            for s in targets:
                if s.topic or s.abstract:
                    cache[(s.tool, s.session_id)] = cache_record(s)
            save_cache(cache_path, cache)
            print(f"  cache updated: {cache_path}")

    # Optional: TUI pick
    to_show = ranked
    if args.pick and ranked:
        picked = tui_pick(ranked)
        if picked is None:
            print("  (cancelled)")
            return 0
        to_show = picked
        print(f"\n  selected {len(to_show)}/{len(ranked)} session(s)")

    print_plan(to_show, use_summary=args.summarize)
    if args.execute and to_show:
        print(f"  launching {len(to_show)} Terminator window(s)...")
        n = launch(to_show)
        print(f"  done — {n}/{len(to_show)} launched\n")
    else:
        print("  (dry-run; nothing was launched — add --execute to launch)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
