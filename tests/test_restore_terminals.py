"""Tests for the platform layer of restore_terminals.py: finding running
sessions and the terminals they run in, and reopening them.

Stdlib only. Run from the repo root:  python3 -m unittest discover tests
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("restore_terminals", ROOT / "restore_terminals.py")
rt = importlib.util.module_from_spec(_spec)
sys.modules["restore_terminals"] = rt  # dataclasses look the module up while building Session
_spec.loader.exec_module(rt)


def session(sid="s1", cwd="/w/a", **kw):
    now = datetime.now(timezone.utc)
    return rt.Session(tool="claude", session_id=sid, path=Path(f"/x/{sid}.jsonl"),
                      cwd=cwd, first_ts=now, last_ts=now, **kw)


def proc(pid, ppid, cmd, tty=None, uid=501):
    return rt.Proc(pid, ppid, uid, tty, cmd.split(), cmd)


class ParsePs(unittest.TestCase):
    def test_fields_and_missing_tty(self):
        out = ("  8891  8695   501 ttys000  claude --dangerously-skip-permissions\n"
               "  1171     1   501 ??       /Users/l/Applications/Ghostty.app/Contents/MacOS/ghostty\n"
               "   bad line\n")
        procs = rt._parse_ps(out)
        self.assertEqual(sorted(procs), [1171, 8891])
        self.assertEqual(procs[8891].argv, ["claude", "--dangerously-skip-permissions"])
        self.assertEqual(procs[8891].tty, "ttys000")
        self.assertEqual(procs[8891].ppid, 8695)
        self.assertIsNone(procs[1171].tty)

    def test_lsof_names(self):
        out = "p10\nfcwd\nn/Users/l/Dev\np11\nn/a b/c\nn/d.jsonl\n"
        self.assertEqual(rt._parse_lsof_names(out),
                         {10: ["/Users/l/Dev"], 11: ["/a b/c", "/d.jsonl"]})


class HostTerminal(unittest.TestCase):
    def chain(self, host_cmd):
        return {
            1: proc(1, 0, "/sbin/launchd"),
            50: proc(50, 1, host_cmd),
            60: proc(60, 50, "/usr/bin/login -flp luis"),
            70: proc(70, 60, "-zsh"),
            80: proc(80, 70, "claude"),
        }

    def test_ghostty(self):
        procs = self.chain("/Users/l/Applications/Ghostty.app/Contents/MacOS/ghostty")
        self.assertEqual(rt._host_terminal(80, procs), "ghostty")

    def test_terminal_app(self):
        procs = self.chain("/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal")
        self.assertEqual(rt._host_terminal(80, procs), "terminal")

    def test_tmux_wins_over_outer_terminal(self):
        procs = self.chain("/Users/l/Applications/Ghostty.app/Contents/MacOS/ghostty")
        procs[60] = proc(60, 50, "tmux: server")
        self.assertEqual(rt._host_terminal(80, procs), "tmux")

    def test_terminator_is_a_python_script(self):
        procs = self.chain("/usr/bin/python3 /usr/bin/terminator -u")
        self.assertEqual(rt._host_terminal(80, procs), "terminator")

    def test_unknown(self):
        procs = self.chain("/usr/sbin/sshd")
        self.assertIsNone(rt._host_terminal(80, procs))


class ClaudeSessionLookup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.pid_dir = base / "sessions"
        self.projects = base / "projects"
        self.pid_dir.mkdir()
        (self.projects / "-w-a").mkdir(parents=True)
        for name, mtime in (("old", 1000), ("newest", 2000)):
            p = self.projects / "-w-a" / f"{name}.jsonl"
            p.write_text("{}\n")
            os.utime(p, (mtime, mtime))
        self.patch = mock.patch.multiple(rt, CLAUDE_PID_DIR=self.pid_dir, CLAUDE_ROOT=self.projects)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_pid_record_beats_argv_and_newest_file(self):
        (self.pid_dir / "42.json").write_text(json.dumps({"pid": 42, "sessionId": "old", "cwd": "/w/a"}))
        self.assertEqual(rt._find_claude_session(42, ["claude", "--resume", "x"], "/w/a"), "old")

    def test_pid_record_for_another_pid_is_ignored(self):
        (self.pid_dir / "42.json").write_text(json.dumps({"pid": 7, "sessionId": "old"}))
        self.assertEqual(rt._find_claude_session(42, ["claude"], "/w/a"), "newest")

    def test_argv_resume_then_newest_file(self):
        self.assertEqual(rt._find_claude_session(42, ["claude", "--resume", "x"], "/w/a"), "x")
        self.assertEqual(rt._find_claude_session(42, ["claude", "--resume=y"], "/w/a"), "y")
        self.assertEqual(rt._find_claude_session(42, ["claude"], "/w/a"), "newest")

    def test_jsonl_path_found_outside_cwd_project(self):
        self.assertEqual(rt._claude_jsonl_path("old", "/elsewhere"),
                         self.projects / "-w-a" / "old.jsonl")
        self.assertIsNone(rt._claude_jsonl_path("missing", "/w/a"))


class MacWindowMatching(unittest.TestCase):
    def test_terminal_by_tty_and_ghostty_by_cwd_in_pid_order(self):
        procs = {10: proc(10, 1, "claude", tty="ttys004"),
                 20: proc(20, 1, "claude", tty="ttys001"),
                 30: proc(30, 1, "claude", tty="ttys002")}
        entries = [
            {"pid": 30, "terminal": "ghostty", "cwd": "/w/a"},
            {"pid": 20, "terminal": "ghostty", "cwd": "/w/a/"},
            {"pid": 10, "terminal": "terminal", "cwd": "/w/b"},
        ]
        ghostty = [{"window_id": "g1", "tab_index": 1, "cwd": "/w/a", "title": "first"},
                   {"window_id": "g1", "tab_index": 2, "cwd": "/w/a", "title": "second"},
                   {"window_id": "g2", "tab_index": 1, "cwd": "/w/c", "title": "other"}]
        terminal = [{"window_id": "77", "tab_index": 1, "tty": "/dev/ttys004", "title": "t"}]
        rt._match_macos_windows(entries, procs, ghostty, terminal)
        by_pid = {e["pid"]: e for e in entries}
        self.assertEqual((by_pid[10]["window_id"], by_pid[10]["tab_index"]), ("77", 1))
        self.assertEqual((by_pid[20]["window_id"], by_pid[20]["tab_index"]), ("g1", 1))
        self.assertEqual((by_pid[30]["window_id"], by_pid[30]["tab_index"]), ("g1", 2))
        self.assertEqual(by_pid[30]["window_title"], "second")

    def test_records_split_on_separators(self):
        out = f"a{rt._US}1{rt._US}/p q{rt._US}title{rt._RS}b{rt._US}2{rt._US}{rt._US}t2{rt._RS}"
        self.assertEqual(rt._split_records(out), [["a", "1", "/p q", "title"], ["b", "2", "", "t2"]])
        self.assertEqual(rt._split_records(None), [])


class GhosttyPlacements(unittest.TestCase):
    def modes(self, sessions, layout, max_splits=4):
        return [(s.session_id, m) for s, m in rt.ghostty_placements(sessions, layout, max_splits)]

    def test_default_layouts_make_no_tabs(self):
        ss = [session(f"s{i}") for i in range(5)]
        self.assertEqual([m for _, m in self.modes(ss, "windows")], ["window"] * 5)
        self.assertEqual([m for _, m in self.modes(ss, "splits")],
                         ["window", "split", "split", "split", "split"])
        self.assertEqual([m for _, m in self.modes(ss, "hybrid", 2)],
                         ["window", "split", "window", "split", "window"])

    def test_windows_ignores_saved_tabs(self):
        ss = [session("a", terminal="ghostty", window_id="A", tab_index=1),
              session("b", terminal="ghostty", window_id="A", tab_index=2)]
        self.assertEqual([m for _, m in self.modes(ss, "windows")], ["window", "window"])

    def test_tabs_layout(self):
        ss = [session(f"s{i}") for i in range(3)]
        self.assertEqual([m for _, m in self.modes(ss, "tabs")], ["window", "tab", "tab"])

    def test_saved_keeps_windows_tabs_and_splits(self):
        ss = [
            session("b2", terminal="ghostty", window_id="B", tab_index=2),
            session("a1", terminal="ghostty", window_id="A", tab_index=1),
            session("b1", terminal="ghostty", window_id="B", tab_index=1),
            session("a1split", terminal="ghostty", window_id="A", tab_index=1),
            session("loose"),
            session("loose2"),
        ]
        self.assertEqual(self.modes(ss, "saved"), [
            ("b1", "window"), ("b2", "tab"),
            ("a1", "window"), ("a1split", "split"),
            ("loose", "window"), ("loose2", "window"),
        ])

    def test_saved_ignores_terminal_app_snapshot(self):
        ss = [session("t", terminal="terminal", window_id="9", tab_index=1), session("u")]
        self.assertEqual([m for _, m in self.modes(ss, "saved")], ["window", "window"])


class ResolveTarget(unittest.TestCase):
    def test_explicit_target_wins(self):
        self.assertEqual(rt._resolve_target("byobu", []), "byobu")

    def test_linux_defaults_to_terminator(self):
        with mock.patch.object(rt, "IS_MACOS", False):
            self.assertEqual(rt._resolve_target("auto", [session(terminal="ghostty")]), "terminator")

    def test_macos_prefers_snapshot_terminal_then_current(self):
        with mock.patch.object(rt, "IS_MACOS", True):
            with mock.patch.dict(os.environ, {"TERM_PROGRAM": "ghostty"}):
                self.assertEqual(rt._resolve_target("auto", [session(terminal="terminal")]), "terminal")
                self.assertEqual(rt._resolve_target("auto", [session()]), "ghostty")
                mixed = [session(terminal="terminal"), session(terminal="ghostty")]
                self.assertEqual(rt._resolve_target("auto", mixed), "ghostty")
            with mock.patch.dict(os.environ, {"TERM_PROGRAM": "Apple_Terminal"}):
                self.assertEqual(rt._resolve_target("auto", [session()]), "terminal")


class PickerRows(unittest.TestCase):
    def test_row_pieces_and_roles(self):
        s = session(cwd=str(Path.home() / "Dev"), topic="Fix the loader", interrupted=True,
                    git_branch="main", pr_number=7, pr_state="open")
        s.search_matches = [{"snippet": "x"}]
        segs = rt._row_segments(s, 120)
        roles = dict((t.strip(), r) for t, r in segs)
        self.assertEqual(roles["!"], "warn")
        self.assertEqual(roles["claude"], "accent")
        self.assertEqual(roles["main"], "ok")
        self.assertEqual(roles["#7o"], "ok")
        self.assertEqual(roles["(1 hits)"], "warn")
        self.assertTrue(set(r for _, r in segs) <= set(rt._ROLE_COLORS) | {""})
        self.assertRegex(rt._row_text(s, 120),
                         r"^! +\d+\.\dd claude ~/Dev  main #7o \(1 hits\)  Fix the loader$")

    def test_closed_pr_is_not_green(self):
        s = session(pr_number=7, pr_state="closed")
        self.assertIn(("#7c".rjust(4), ""), [(t, r) for t, r in rt._row_segments(s, 120)])

    def test_label_is_clipped_to_width(self):
        s = session(topic="x" * 200)
        self.assertLessEqual(len(rt._row_text(s, 80)), 80)

    def test_no_color(self):
        self.assertTrue(rt._tui_colors_enabled({}, True))
        self.assertFalse(rt._tui_colors_enabled({"NO_COLOR": "1"}, True))
        self.assertTrue(rt._tui_colors_enabled({"NO_COLOR": ""}, True))
        self.assertFalse(rt._tui_colors_enabled({}, False))


class ProgressColors(unittest.TestCase):
    def test_paint_on_a_terminal(self):
        with mock.patch.object(rt, "_stdout_colors", return_value=True):
            self.assertEqual(rt._paint(3, "ok"), "\033[32m3\033[0m")
            self.assertEqual(rt._paint("x", "bold"), "\033[1mx\033[0m")
            self.assertEqual(rt._paint("x", ""), "x")

    def test_plain_when_colour_is_off(self):
        with mock.patch.object(rt, "_stdout_colors", return_value=False):
            self.assertEqual(rt._paint("x", "warn"), "x")

    def test_off_when_piped_no_color_or_dumb(self):
        with mock.patch.object(sys.stdout, "isatty", return_value=False):
            self.assertFalse(rt._stdout_colors())
        with mock.patch.object(sys.stdout, "isatty", return_value=True):
            with mock.patch.dict(os.environ, {"NO_COLOR": "1", "TERM": "xterm"}):
                self.assertFalse(rt._stdout_colors())
            with mock.patch.dict(os.environ, {"TERM": "dumb"}):
                os.environ.pop("NO_COLOR", None)
                self.assertFalse(rt._stdout_colors())
            with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
                os.environ.pop("NO_COLOR", None)
                self.assertTrue(rt._stdout_colors())

    def test_only_normal_colours(self):
        self.assertEqual(set(rt._ANSI.values()), {"32", "33", "36", "1"})


class SnapshotCompat(unittest.TestCase):
    def test_linux_snapshot_without_new_fields_loads(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.jsonl"
            p.write_text("")
            with mock.patch.object(rt, "parse_claude", return_value=session("s1")):
                [s] = rt.snapshot_to_sessions([{"tool": "claude", "jsonl_path": str(p),
                                                "workspace": " 1 ", "window_title": "t"}])
        self.assertEqual(s.workspace, " 1 ")
        self.assertIsNone(s.terminal)
        self.assertIsNone(s.window_id)


class SnapshotLiveProcess(unittest.TestCase):
    """Starts a real process named `claude` and finds it, through /proc on
    Linux and ps/lsof on macOS."""

    def test_finds_fake_claude_process(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(os.path.realpath(d))
            work = base / "work"
            work.mkdir()
            child = subprocess.Popen(["bash", "-c", "exec -a claude sleep 30"], cwd=work)
            try:
                pid_dir = base / "sessions"
                pid_dir.mkdir()
                (pid_dir / f"{child.pid}.json").write_text(
                    json.dumps({"pid": child.pid, "sessionId": "fake-sid", "cwd": str(work)}))
                projects = base / "projects" / rt._encode_cwd_to_project(str(work))
                projects.mkdir(parents=True)
                (projects / "fake-sid.jsonl").write_text("{}\n")
                with mock.patch.multiple(rt, CLAUDE_PID_DIR=pid_dir, CLAUDE_ROOT=base / "projects"):
                    entry = None
                    for _ in range(20):  # exec -a takes effect a moment after spawn
                        found = [e for e in rt.snapshot_active(with_windows=False)
                                 if e["pid"] == child.pid]
                        if found:
                            entry = found[0]
                            break
                        time.sleep(0.1)
            finally:
                child.kill()
                child.wait()
        self.assertIsNotNone(entry, "fake claude process not found")
        self.assertEqual(entry["session_id"], "fake-sid")
        self.assertEqual(entry["cwd"], str(work))
        self.assertEqual(entry["jsonl_path"], str(projects / "fake-sid.jsonl"))


if __name__ == "__main__":
    unittest.main()
