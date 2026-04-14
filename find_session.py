#!/usr/bin/env python3
"""
find-session — search Claude/Codex sessions by content.

Finds sessions that mention a git branch, repo name, Jira ticket, filename,
or any text. Matches against the full transcript (user messages, assistant
replies, tool outputs). Also resolves each session's cwd to a git repo/branch
when possible.

Usage:
    find-session <pattern> [days]

Examples:
    find-session feature/notification-fix
    find-session epic/platform 14
    find-session DSBF-491
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


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

_git_cache: dict[str, dict] = {}


def git_info(cwd: str | None) -> dict:
    """Resolve cwd to git remote + branch. Cached per directory."""
    if not cwd:
        return {}
    if cwd in _git_cache:
        return _git_cache[cwd]
    info: dict = {}
    if not Path(cwd).is_dir():
        _git_cache[cwd] = info
        return info
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            info["remote"] = r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            info["branch"] = r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    _git_cache[cwd] = info
    return info


# ---------------------------------------------------------------------------
# Session search result
# ---------------------------------------------------------------------------

@dataclass
class Match:
    timestamp: str
    role: str
    snippet: str      # truncated text around the match


@dataclass
class SessionHit:
    tool: str
    session_id: str
    path: Path
    cwd: str | None
    first_ts: datetime | None
    last_ts: datetime | None
    git: dict                  # {remote, branch} if available
    matches: list[Match] = field(default_factory=list)
    user_message_count: int = 0

    @property
    def age_days(self) -> float:
        if not self.last_ts:
            return float("inf")
        return (datetime.now(timezone.utc) - self.last_ts).total_seconds() / 86400.0


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _truncate(s: str, n: int = 200) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _snippet_around(text: str, pattern: re.Pattern, ctx: int = 80) -> str:
    """Extract a snippet centered on the first match."""
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


# ---------------------------------------------------------------------------
# Session searchers
# ---------------------------------------------------------------------------


def _extract_text_claude(d: dict) -> tuple[str, str, str]:
    """Return (timestamp, role, text) from a Claude JSONL line."""
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


def _extract_text_codex(d: dict) -> tuple[str, str, str]:
    """Return (timestamp, role, text) from a Codex JSONL line."""
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
    # nested content list (response_item/message)
    content = p.get("content") if isinstance(p, dict) else None
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                for key in ("text", "content"):
                    v = c.get(key)
                    if isinstance(v, str):
                        text += v + "\n"
    return ts, role, text


def search_file(
    path: Path,
    tool: str,
    pattern: re.Pattern,
    cutoff: datetime,
    max_matches: int = 10,
) -> SessionHit | None:
    """Search one JSONL session file for pattern matches."""
    extractor = _extract_text_claude if tool == "claude" else _extract_text_codex
    cwd: str | None = None
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    session_id = path.stem
    matches: list[Match] = []
    user_msg_count = 0
    cwd_matched = False
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
                # extract cwd
                if cwd is None:
                    if tool == "claude" and d.get("cwd"):
                        cwd = d["cwd"]
                    elif tool == "codex" and d.get("type") == "session_meta":
                        p = d.get("payload") or {}
                        cwd = p.get("cwd") if isinstance(p, dict) else None
                        session_id = (p.get("id") if isinstance(p, dict) else None) or session_id
                # timestamps
                ts = _parse_ts(d.get("timestamp"))
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                # fast reject: raw line doesn't contain pattern at all → skip parsing
                if not pattern.search(line):
                    continue
                timestamp, role, text = extractor(d)
                if role in ("user", "user_message"):
                    user_msg_count += 1
                if text and pattern.search(text) and len(matches) < max_matches:
                    matches.append(Match(
                        timestamp=timestamp,
                        role=role,
                        snippet=_snippet_around(text, pattern),
                    ))
    except OSError:
        return None
    if not last_ts or last_ts < cutoff:
        return None
    # also check if cwd path or git remote matches
    if cwd and pattern.search(cwd):
        cwd_matched = True
    gi = git_info(cwd) if cwd else {}
    if gi.get("remote") and pattern.search(gi["remote"]):
        cwd_matched = True
    if gi.get("branch") and pattern.search(gi["branch"]):
        cwd_matched = True
    if not matches and not cwd_matched:
        return None
    return SessionHit(
        tool=tool,
        session_id=session_id,
        path=path,
        cwd=cwd,
        first_ts=first_ts,
        last_ts=last_ts,
        git=gi,
        matches=matches,
        user_message_count=user_msg_count,
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_and_search(
    pattern: re.Pattern,
    cutoff: datetime,
    tools: set[str],
    max_matches_per_session: int = 10,
) -> list[SessionHit]:
    hits: list[SessionHit] = []
    if "claude" in tools and CLAUDE_ROOT.exists():
        for proj in CLAUDE_ROOT.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                if "subagents" in f.parts:
                    continue
                h = search_file(f, "claude", pattern, cutoff, max_matches_per_session)
                if h:
                    hits.append(h)
    if "codex" in tools and CODEX_ROOT.exists():
        for f in CODEX_ROOT.rglob("rollout-*.jsonl"):
            h = search_file(f, "codex", pattern, cutoff, max_matches_per_session)
            if h:
                hits.append(h)
    hits.sort(key=lambda x: x.last_ts or datetime.min.replace(tzinfo=timezone.utc),
              reverse=True)
    return hits


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def short_cwd(cwd: str | None) -> str:
    if not cwd:
        return "?"
    home = str(HOME)
    if cwd.startswith(home):
        return "~" + cwd[len(home):]
    return cwd


def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "?"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def resume_cmd(hit: SessionHit) -> str:
    if hit.tool == "claude":
        return f"claude --resume {hit.session_id}"
    return f"codex resume {shlex.quote(str(hit.path))}"


def print_hits(hits: list[SessionHit], pattern_str: str, context: int) -> None:
    if not hits:
        print(f"\n  No sessions match '{pattern_str}'.\n")
        return
    print(f"\n  {len(hits)} session(s) match '{pattern_str}':\n")
    for i, h in enumerate(hits, 1):
        git_line = ""
        if h.git.get("remote"):
            git_line += f"  remote={h.git['remote']}"
        if h.git.get("branch"):
            git_line += f"  branch={h.git['branch']}"
        print(f"  ── {i}/{len(hits)} ──")
        print(f"    tool    : {h.tool}")
        print(f"    cwd     : {short_cwd(h.cwd)}")
        if git_line:
            print(f"    git     :{git_line}")
        print(f"    session : {h.session_id}")
        print(f"    active  : {fmt_dt(h.first_ts)} → {fmt_dt(h.last_ts)}  "
              f"({h.age_days:.1f}d ago)")
        print(f"    matches : {len(h.matches)}")
        print(f"    resume  : {resume_cmd(h)}")
        for j, m in enumerate(h.matches[:context]):
            ts_short = m.timestamp[:16] if m.timestamp else ""
            print(f"      [{j + 1}] {ts_short} [{m.role}] {_truncate(m.snippet, 160)}")
        if len(h.matches) > context:
            print(f"      ... and {len(h.matches) - context} more")
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="find-session",
        description="Search Claude/Codex sessions by content (branch, repo, ticket, text).",
        epilog=(
            "Examples:\n"
            "  find-session feature/notification-fix\n"
            "  find-session DSBF-491 14\n"
            "  find-session 'epic/platform.*merge' 30\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("pattern",
                    help="regex to search for (branch name, repo, ticket, any text)")
    ap.add_argument("days", nargs="?", type=int, default=30,
                    help="lookback window in days (default: 30)")
    ap.add_argument("--context", type=int, default=5,
                    help=argparse.SUPPRESS)  # max match snippets to show per session
    ap.add_argument("--only", choices=["claude", "codex", "all"], default="all",
                    help=argparse.SUPPRESS)
    args = ap.parse_args()

    try:
        pat = re.compile(args.pattern, re.IGNORECASE)
    except re.error as e:
        print(f"Invalid regex '{args.pattern}': {e}", file=sys.stderr)
        return 1

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    tools = {"claude", "codex"} if args.only == "all" else {args.only}

    print(f"  searching {args.days} days of sessions for /{args.pattern}/i ...")
    hits = discover_and_search(pat, cutoff, tools)
    print_hits(hits, args.pattern, args.context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
