"""Tests for the platform layer of restore_terminals.py: finding running
sessions and the terminals they run in, and reopening them.

Stdlib only. Run from the repo root:  python3 -m unittest discover tests
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # restore_terminals imports termui from beside it
import termui  # noqa: E402
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


class Truncate(unittest.TestCase):
    def test_matches_termui_clip(self):
        for text, n in (("a  b\nc", 10), ("abcdefghij", 5), (None, 5), ("", 5), ("x" * 200, 120)):
            self.assertEqual(rt._truncate(text, n), termui.clip(text, n))
        self.assertEqual(len(rt._truncate("y" * 500)), 120)


class ParsePs(unittest.TestCase):
    def test_fields_and_missing_tty(self):
        out = ("  8891  8695   501 ttys000  Tue Oct 27 20:45:15 2026     claude --dangerously-skip-permissions\n"
               "  1171     1   501 ??       Mon Oct  5 09:01:02 2026     /Users/l/Applications/Ghostty.app/Contents/MacOS/ghostty\n"
               "   bad line\n")
        procs = rt._parse_ps(out)
        self.assertEqual(sorted(procs), [1171, 8891])
        self.assertEqual(procs[8891].argv, ["claude", "--dangerously-skip-permissions"])
        self.assertEqual(procs[8891].tty, "ttys000")
        self.assertEqual(procs[8891].ppid, 8695)
        self.assertIsNone(procs[1171].tty)
        self.assertEqual(procs[8891].start, time.mktime((2026, 10, 27, 20, 45, 15, 0, 0, -1)))
        self.assertEqual(procs[1171].start, time.mktime((2026, 10, 5, 9, 1, 2, 0, 0, -1)))

    def test_lsof_names(self):
        out = "p10\nfcwd\nn/Users/l/Dev\np11\nn/a b/c\nn/d.jsonl\n"
        self.assertEqual(rt._parse_lsof_names(out),
                         {10: ["/Users/l/Dev"], 11: ["/a b/c", "/d.jsonl"]})


class I3Move(unittest.TestCase):
    def test_workspace_name_is_quoted_exactly(self):
        self.assertEqual(rt._i3_move_command("0x10", " 1 "), '[id=0x10] move to workspace " 1 "')
        self.assertEqual(rt._i3_move_command("0x10", '2: "web"'),
                         '[id=0x10] move to workspace "2: \\"web\\""')
        self.assertEqual(rt._i3_move_command("0x10", "a\\b"), '[id=0x10] move to workspace "a\\\\b"')


class LsofCalls(unittest.TestCase):
    def test_lsof_skips_name_lookups(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "p7\nn/x/rollout-1.jsonl\n", "")
        with mock.patch.object(rt.Path, "is_dir", return_value=False), \
             mock.patch.object(rt.subprocess, "run", side_effect=fake_run):
            self.assertEqual(rt._proc_open_files(7), ["/x/rollout-1.jsonl"])
            rt._proc_cwds([7, 8])
        self.assertEqual(len(calls), 2)
        for argv in calls:
            self.assertEqual(argv[0], "lsof")
            self.assertIn("-n", argv)
            self.assertIn("-P", argv)


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
        rec = rt._claude_pid_record(42)
        self.assertEqual(rt._find_claude_session(42, ["claude", "--resume", "x"], "/w/a", rec), "old")

    def test_pid_record_for_another_pid_is_ignored(self):
        (self.pid_dir / "42.json").write_text(json.dumps({"pid": 7, "sessionId": "old"}))
        self.assertIsNone(rt._claude_pid_record(42))
        self.assertEqual(rt._find_claude_session(42, ["claude", "--resume", "x"], "/w/a", None), "x")

    def test_argv_resume_then_newest_file(self):
        self.assertEqual(rt._find_claude_session(42, ["claude", "--resume", "x"], "/w/a"), "x")
        self.assertEqual(rt._find_claude_session(42, ["claude", "--resume=y"], "/w/a"), "y")
        self.assertEqual(rt._find_claude_session(42, ["claude"], "/w/a"), "newest")

    def test_no_guess_when_claude_writes_pid_records(self):
        # `claude agents` has no record of its own; it is not the newest session in its folder
        (self.pid_dir / "7.json").write_text(json.dumps({"pid": 7, "sessionId": "other"}))
        self.assertIsNone(rt._find_claude_session(42, ["claude", "agents"], "/w/a"))
        self.assertIsNone(rt._find_claude_session(42, ["claude"], "/w/a"))

    def test_fork_names_its_session_and_a_resume_path_its_file(self):
        argv = ["/x/claude/versions/2.1.312", "--session-id", "new", "--fork-session",
                "--resume", "/p/-w-a/old.jsonl"]
        self.assertEqual(rt._find_claude_session(42, argv, "/w/a"), "new")
        self.assertEqual(rt._find_claude_session(42, ["claude", "--resume", "/p/-w-a/old.jsonl"], "/w/a"),
                         "old")

    def test_hosted_command_line_is_not_the_hosts_session(self):
        (self.pid_dir / "7.json").write_text(json.dumps({"pid": 7, "sessionId": "other"}))
        argv = ["claude", "--bg-pty-host", "/tmp/s.sock", "210", "60", "--",
                "/x/claude/versions/2.1.312", "--session-id", "new"]
        self.assertIsNone(rt._find_claude_session(42, argv, "/w/a"))

    def test_record_older_than_the_process_is_ignored(self):
        started = 1_800_000_000.0
        (self.pid_dir / "42.json").write_text(
            json.dumps({"pid": 42, "sessionId": "left-behind", "startedAt": (started - 3600) * 1000}))
        self.assertIsNone(rt._claude_pid_record(42, started))
        self.assertEqual(rt._claude_pid_record(42)["sessionId"], "left-behind")  # start unknown

    def test_record_written_just_after_start_is_used(self):
        started = 1_800_000_000.0
        (self.pid_dir / "42.json").write_text(
            json.dumps({"pid": 42, "sessionId": "live", "startedAt": (started + 1.4) * 1000}))
        self.assertEqual(rt._claude_pid_record(42, started)["sessionId"], "live")
        (self.pid_dir / "42.json").write_text(
            json.dumps({"pid": 42, "sessionId": "live", "startedAt": (started - 2) * 1000}))
        self.assertEqual(rt._claude_pid_record(42, started)["sessionId"], "live")  # clock rounding

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

    def ghostty(self, *terms):
        return [{"window_id": w, "tab_index": 1, "cwd": cwd, "title": title} for w, cwd, title in terms]

    def test_title_beats_folder_and_order(self):
        procs = {10: proc(10, 1, "claude", tty="ttys001"), 20: proc(20, 1, "claude", tty="ttys002")}
        entries = [{"pid": 10, "terminal": "ghostty", "cwd": "/w/a"},
                   {"pid": 20, "terminal": "ghostty", "cwd": "/w/a"}]
        terms = self.ghostty(("W1", "/w/a", "✳ Second task"), ("W2", "/w/a", "◐ First task"))
        rt._match_macos_windows(entries, procs, terms, [], titles={10: "First task", 20: "Second task"})
        self.assertEqual([e["window_id"] for e in entries], ["W2", "W1"])

    def test_agent_terminal_beats_plain_shell_in_the_same_folder(self):
        procs = {10: proc(10, 1, "claude", tty="ttys001")}
        entries = [{"pid": 10, "terminal": "ghostty", "cwd": "/w/a"}]
        terms = self.ghostty(("W1", "/w/a", "zsh"), ("W2", "/w/a", "✳ Something"))
        rt._match_macos_windows(entries, procs, terms, [])
        self.assertEqual(entries[0]["window_id"], "W2")

    def test_background_session_without_a_tty_gets_no_window(self):
        procs = {10: proc(10, 1, "claude --bg-pty-host /tmp/x")}
        entries = [{"pid": 10, "terminal": "ghostty", "cwd": "/w/a"}]
        rt._match_macos_windows(entries, procs, self.ghostty(("W1", "/w/a", "✳ x")), [],
                                titles={10: "x"})
        self.assertNotIn("window_id", entries[0])

    def test_folders_match_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            real = Path(os.path.realpath(d)) / "real"
            real.mkdir()
            link = Path(os.path.realpath(d)) / "link"
            link.symlink_to(real)
            procs = {10: proc(10, 1, "claude", tty="ttys001")}
            entries = [{"pid": 10, "terminal": "ghostty", "cwd": str(real)}]
            rt._match_macos_windows(entries, procs, self.ghostty(("W1", str(link), "✳ x")), [])
        self.assertEqual(entries[0].get("window_id"), "W1")

    def test_bare_title_and_agent_title(self):
        self.assertEqual(rt._bare_title("✳ TLA+ spec"), "TLA+ spec")
        self.assertEqual(rt._bare_title('⠂ "MDM fixes"'), '"MDM fixes"')
        self.assertEqual(rt._bare_title("~/Dev"), "~/Dev")
        self.assertTrue(rt._agent_title("◑ Show-must-go-on"))
        self.assertFalse(rt._agent_title("restore"))
        self.assertFalse(rt._agent_title(""))

    def test_session_title_prefers_the_last_custom_title(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.jsonl"
            lines = [{"type": "ai-title", "aiTitle": "First AI title"},
                     {"type": "user", "message": {"role": "user", "content": "aiTitle in text"}},
                     {"type": "ai-title", "aiTitle": "Second AI title"}]
            path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
            self.assertEqual(rt._session_title(str(path)), "Second AI title")
            with path.open("a") as f:
                f.write(json.dumps({"type": "custom-title", "customTitle": "Renamed"}) + "\n")
                f.write(json.dumps({"type": "ai-title", "aiTitle": "Later AI title"}) + "\n")
            self.assertEqual(rt._session_title(str(path)), "Renamed")
        self.assertIsNone(rt._session_title(None))
        self.assertIsNone(rt._session_title("/nonexistent.jsonl"))

    def test_terminal_tabs_whose_shell_exited_are_left_out(self):
        us, rs = rt._US, rt._RS
        out = (f"736{us}1{us}/dev/ttys014{us}old{us}0{rs}"
               f"770{us}1{us}/dev/ttys014{us}new{us}2{rs}"
               f"770{us}2{us}/dev/ttys015{us}other{us}1{rs}")
        with mock.patch.object(rt, "_osascript", return_value=out):
            tabs = rt._terminal_app_tabs()
        self.assertEqual([(t["window_id"], t["tab_index"]) for t in tabs], [("770", 1), ("770", 2)])

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


class ShellLine(unittest.TestCase):
    def test_paths_under_home_use_a_tilde(self):
        with mock.patch.object(rt, "HOME", Path("/Users/u")):
            self.assertEqual(rt._shell_quote_path("/Users/u"), "~")
            self.assertEqual(rt._shell_quote_path("/Users/u/Dev/x"), "~/Dev/x")
            self.assertEqual(rt._shell_quote_path("/Users/u/My Projects/x"), "~/'My Projects/x'")
            self.assertEqual(rt._shell_quote_path("/Users/user2/x"), "/Users/user2/x")
            self.assertEqual(rt._shell_quote_path("/opt/a b"), "'/opt/a b'")
            s = session(sid="abc", cwd="/Users/u/Dev/x")
            self.assertEqual(rt._shell_line(s), "cd -- ~/Dev/x && claude --resume abc")

    def test_the_shell_expands_it(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            home = Path(os.path.realpath(d))
            (home / "My Projects").mkdir()
            with mock.patch.object(rt, "HOME", home):
                line = rt._shell_quote_path(str(home / "My Projects"))
            for shell in ("/bin/sh", "/bin/bash", "/bin/zsh"):
                if not Path(shell).exists():
                    continue
                out = subprocess.run([shell, "-c", f"cd -- {line} && pwd"], capture_output=True,
                                     text=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
                self.assertEqual(out.stdout.strip(), str(home / "My Projects"), shell)


class MacLaunchers(unittest.TestCase):
    """What the Ghostty and Terminal.app launchers send, and how they report."""

    def sessions(self):
        return [session(sid="a", cwd=str(Path.home() / "Dev/a"), topic="Task A"),
                session(sid="b", cwd="/opt/b c", topic="Task B")]

    def run_launcher(self, launch, reply):
        sent = {}

        def fake(script, *argv, timeout=15):
            sent["script"], sent["argv"] = script, list(argv)
            return reply
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(rt, "_osascript", side_effect=fake), \
             mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            n = launch(self.sessions())
        return n, sent, out.getvalue(), err.getvalue()

    def test_terminal_app_arguments(self):
        n, sent, out, err = self.run_launcher(rt.launch_terminal_app, "2")
        self.assertEqual(n, 2)
        self.assertIs(sent["script"], rt._TERMINAL_LAUNCH)
        self.assertEqual(sent["argv"], ["cd -- ~/Dev/a && claude --resume a", "Task A",
                                        "cd -- '/opt/b c' && claude --resume b", "Task B"])
        self.assertEqual(out.count("✓"), 2)
        self.assertEqual(err, "")

    def test_ghostty_arguments(self):
        launch = lambda ss: rt.launch_ghostty(ss, layout="tabs", max_splits=4)
        n, sent, out, _ = self.run_launcher(launch, "2")
        self.assertIs(sent["script"], rt._GHOSTTY_LAUNCH)
        self.assertEqual(sent["argv"], [
            "window", str(Path.home() / "Dev/a"), "cd -- ~/Dev/a && claude --resume a",
            "tab", "/opt/b c", "cd -- '/opt/b c' && claude --resume b"])
        self.assertEqual(n, 2)

    def test_failure_part_way_is_reported_per_session(self):
        n, _, out, err = self.run_launcher(rt.launch_terminal_app, "1|Terminal got an error: boom")
        self.assertEqual(n, 1)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertTrue(lines[0].lstrip().startswith("✓"), lines)
        self.assertTrue(lines[1].lstrip().startswith("✗"), lines)
        self.assertIn("opened 1/2: Terminal got an error: boom", err)

    def test_osascript_failure(self):
        launch = lambda ss: rt.launch_ghostty(ss, layout="windows", max_splits=4)
        n, _, out, err = self.run_launcher(launch, None)
        self.assertEqual(n, 0)
        self.assertNotIn("✓", out)
        self.assertIn("Ghostty opened 0/2: osascript failed", err)

    @unittest.skipUnless(shutil.which("osacompile"), "macOS only")
    def test_launch_scripts_compile(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("_GHOSTTY_LAUNCH", "_TERMINAL_LAUNCH", "_GHOSTTY_LIST", "_TERMINAL_LIST"):
                src = Path(d) / f"{name}.applescript"
                src.write_text(getattr(rt, name))
                r = subprocess.run(["osacompile", "-o", str(Path(d) / f"{name}.scpt"), str(src)],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")


class ResolveTarget(unittest.TestCase):
    def test_explicit_target_wins(self):
        self.assertEqual(rt._resolve_target("byobu", []), "byobu")

    def test_linux_defaults_to_terminator(self):
        with mock.patch.object(rt, "IS_MACOS", False):
            self.assertEqual(rt._resolve_target("auto", [session(terminal="ghostty")]), "terminator")

    def test_macos_uses_the_terminal_found_among_the_parents(self):
        with mock.patch.object(rt, "IS_MACOS", True), \
             mock.patch.dict(os.environ, {"TERM_PROGRAM": "tmux"}):
            with mock.patch.object(rt, "_current_terminal", return_value="ghostty"):
                self.assertEqual(rt._resolve_target("auto", [session()]), "ghostty")
            with mock.patch.object(rt, "_current_terminal", return_value="terminal"):
                self.assertEqual(rt._resolve_target("auto", [session()]), "terminal")
            err = io.StringIO()
            with mock.patch.object(rt, "_current_terminal", return_value="iterm"), \
                 mock.patch.object(sys, "stderr", err):
                self.assertEqual(rt._resolve_target("auto", [session()]), "terminal")
            self.assertIn("cannot open windows in iterm", err.getvalue())

    def test_current_terminal_looks_past_tmux_to_its_client(self):
        procs = {
            1: proc(1, 0, "/sbin/launchd"),
            50: proc(50, 1, "/Users/l/Applications/Ghostty.app/Contents/MacOS/ghostty"),
            60: proc(60, 50, "-zsh"),
            70: proc(70, 60, "tmux attach"),             # the client, inside Ghostty
            80: proc(80, 1, "tmux new-session -d"),      # the server, a daemon
            90: proc(90, 80, "-zsh"),
            os.getpid(): proc(os.getpid(), 90, "python3 restore"),
        }
        with mock.patch.object(rt, "_list_procs", return_value=procs), \
             mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,80,0"}), \
             mock.patch.object(rt.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "70\n", "")):
            self.assertEqual(rt._current_terminal(), "ghostty")
        with mock.patch.object(rt, "_list_procs", return_value=procs), \
             mock.patch.dict(os.environ, {"TMUX": ""}):
            self.assertEqual(rt._current_terminal(), "tmux")

    def test_macos_prefers_snapshot_terminal_then_current(self):
        with mock.patch.object(rt, "IS_MACOS", True), \
             mock.patch.object(rt, "_current_terminal", return_value=None):
            with mock.patch.dict(os.environ, {"TERM_PROGRAM": "ghostty"}):
                self.assertEqual(rt._resolve_target("auto", [session(terminal="terminal")]), "terminal")
                self.assertEqual(rt._resolve_target("auto", [session()]), "ghostty")
                mixed = [session(terminal="terminal"), session(terminal="ghostty")]
                self.assertEqual(rt._resolve_target("auto", mixed), "ghostty")
            with mock.patch.dict(os.environ, {"TERM_PROGRAM": "Apple_Terminal"}):
                self.assertEqual(rt._resolve_target("auto", [session()]), "terminal")


class PromptText(unittest.TestCase):
    """Only what the person typed counts as a prompt for display."""

    def test_inserted_entries_are_not_prompts(self):
        for d, text in [
            ({"isMeta": True}, "Base directory for this skill: ..."),
            ({"isCompactSummary": True}, "This session is being continued from a previous ..."),
            ({"isVisibleInTranscriptOnly": True}, "<artifact-content-authored-by-others/> ..."),
            ({"origin": {"kind": "task-notification"}}, "<task-notification> <task-id>1</task-id>"),
            ({}, "<local-command-stdout>Compacted</local-command-stdout>"),
            ({}, "<local-command-caveat>The command below was run ...</local-command-caveat>"),
            ({}, "<bash-input>ls</bash-input>"),
            ({}, "<system-reminder>...</system-reminder>"),
            ({}, "[Request interrupted by user for tool use]"),
        ]:
            self.assertIsNone(rt._prompt_text(d, text), text)

    def test_typed_text_and_commands(self):
        self.assertEqual(rt._prompt_text({"origin": {"kind": "human"}}, "fix it"), "fix it")
        self.assertEqual(rt._prompt_text({}, "  plain old prompt"), "  plain old prompt")
        cmd = ("<command-message>torq:check</command-message>\n<command-name>/torq:check</command-name>"
               "\n<command-args>52180</command-args>")
        self.assertEqual(rt._prompt_text({}, cmd), "/torq:check 52180")
        self.assertEqual(rt._prompt_text({}, "<command-name>/compact</command-name>"), "/compact")

    def write(self, entries):
        d = tempfile.mkdtemp()
        path = Path(d) / "s.jsonl"
        now = datetime.now(timezone.utc)
        with path.open("w") as f:
            for i, (extra, role, content) in enumerate(entries):
                ts = (now - timedelta(minutes=60 - i)).isoformat()
                f.write(json.dumps({"type": role, "timestamp": ts, "cwd": d, **extra,
                                    "message": {"role": role, "content": content}}) + "\n")
        return path

    def test_parse_keeps_counts_and_clear_detection(self):
        ok = [{"type": "text", "text": "ok"}]
        path = self.write([
            ({"isCompactSummary": True}, "user", "This session is being continued ..."),
            ({}, "user", "<command-name>/model</command-name>"),
            ({}, "assistant", ok),
            ({"origin": {"kind": "human"}}, "user", "the real first prompt"),
            ({}, "assistant", ok),
            ({"origin": {"kind": "task-notification"}}, "user", "<task-notification>x</task-notification>"),
            ({}, "user", "<command-name>/clear</command-name>"),
        ])
        s = rt.parse_claude(path)
        self.assertEqual(s.first_user_prompt, "the real first prompt")
        self.assertEqual(s.last_user_prompt, "/clear")
        self.assertTrue(s.closed_with_clear)
        self.assertEqual(s.user_message_count, 5)   # counts every user entry, as before
        self.assertNotIn("task-notification", s.last_hour_transcript)

    def test_a_session_of_only_commands_shows_the_first_command(self):
        path = self.write([({}, "user", "<command-name>/mcp</command-name>"),
                           ({}, "user", "<command-name>/model</command-name>")])
        s = rt.parse_claude(path)
        self.assertEqual((s.first_user_prompt, s.last_user_prompt), ("/mcp", "/model"))

    def test_agent_title_prefers_rename_and_the_last_of_each(self):
        path = self.write([({}, "user", "hello")])
        with path.open("a") as f:
            for rec in ({"type": "ai-title", "aiTitle": "First guess"},
                        {"type": "ai-title", "aiTitle": "Loader retries"},
                        {"type": "custom-title", "customTitle": "my name"},
                        {"type": "custom-title", "customTitle": "loader v2"}):
                f.write(json.dumps({**rec, "sessionId": "s"}) + "\n")
        s = rt.parse_claude(path)
        self.assertEqual((s.agent_title, s.renamed), ("loader v2", True))

    def test_title_the_agent_chose(self):
        path = self.write([({}, "user", "hello")])
        with path.open("a") as f:
            f.write(json.dumps({"type": "ai-title", "aiTitle": "Loader retries"}) + "\n")
        s = rt.parse_claude(path)
        self.assertEqual((s.agent_title, s.renamed), ("Loader retries", False))
        self.assertIsNone(rt.parse_claude(self.write([({}, "user", "hi")])).agent_title)


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
        self.assertEqual(roles["(1 hit)"], "warn")
        self.assertTrue(set(r for _, r in segs) <= set(termui.ROLE_COLORS) | {""})
        self.assertRegex(rt._row_text(s, 120),
                         r"^! +\d+\.\dd claude ~/Dev  main #7o \(1 hit\)  Fix the loader$")

    def test_hit_count_is_pluralised(self):
        s = session()
        s.search_matches = [{"snippet": "a"}, {"snippet": "b"}]
        self.assertIn(" (2 hits)", rt._row_text(s, 120))

    def test_closed_pr_is_not_green(self):
        s = session(pr_number=7, pr_state="closed")
        self.assertIn(("#7c".rjust(4), ""), [(t, r) for t, r in rt._row_segments(s, 120)])

    def test_label_is_clipped_to_width(self):
        s = session(topic="x" * 200)
        self.assertLessEqual(len(rt._row_text(s, 80)), 80)

    def test_agent_title_comes_first_then_the_topic(self):
        s = session(cwd="/w/a", agent_title="loader v2", topic="Fix the loader retries")
        segs = rt._row_segments(s, 120)
        self.assertIn(("loader v2", "bold"), segs)
        self.assertTrue(rt._row_text(s, 120).endswith("  loader v2 · Fix the loader retries"))

    def test_same_title_twice_is_shown_once(self):
        s = session(cwd="/w/a", agent_title="Fix the loader", topic="fix the loader ")
        self.assertTrue(rt._row_text(s, 120).endswith("  Fix the loader"))

    def test_long_agent_title_is_kept_whole_when_the_row_has_room(self):
        s = session(cwd="/w/a", agent_title="Webhook retries flooding slow endpoints",
                    topic="Webhook retry backoff fix")
        self.assertTrue(rt._row_text(s, 100).endswith(
            "  Webhook retries flooding slow endpoints · Webhook retry backoff fix"))
        # short of room, the topic is cut before the agent's title
        text = rt._row_text(s, 85)
        self.assertIn("  Webhook retries flooding slow endpoints · Webhook", text)
        self.assertTrue(text.endswith("…"), text)

    def test_both_titles_fit_the_width(self):
        s = session(cwd="/w/a", agent_title="a" * 90, topic="b" * 90)
        for width in (60, 80, 120):
            text = rt._row_text(s, width)
            self.assertLessEqual(len(text), width)
            self.assertIn("a" * 10, text)


class DetailsPage(unittest.TestCase):
    def details(self, s, width=100):
        lines = rt._detail_lines(s, width)
        return lines, ["".join(t for t, _ in line) for line in lines]

    def test_sections_and_fields(self):
        s = session(cwd=str(Path.home() / "Dev"), topic="Fix the loader", interrupted=True,
                    abstract="A summary. " * 30, first_user_prompt="start here",
                    last_user_prompt="and finish", git_branch="main", pr_number=7,
                    pr_state="open", pr_title="Loader", pr_url="https://x/7",
                    terminal="ghostty", window_id="tab-group-1", tab_index=2)
        s.last_hour_transcript = "[user] please do it\n\n[assistant] done it"
        lines, text = self.details(s)
        self.assertEqual(text[0], " Fix the loader")
        for heading in ("Session", "Where", "Summary", "Prompts", "Resume"):
            self.assertIn(f" {heading}", text)
        joined = "\n".join(text)
        self.assertIn("interrupted: the agent had the last turn", joined)
        self.assertIn("ghostty window tab-group-1, tab 2", joined)
        self.assertIn(f"claude --resume {s.session_id}", joined)
        self.assertIn("Last hour of conversation (last 2 of 2 turns)", joined)
        roles = {line[0][0].strip(): line[0][1] for line in lines if line}
        self.assertEqual(roles["user"], "ok")
        self.assertEqual(roles["assistant"], "accent")
        status = [line for line in lines if line and line[0][0].strip() == "status"][0]
        self.assertEqual(status[1][1], "warn")

    def test_both_titles_head_the_page(self):
        s = session(agent_title="loader v2", renamed=True, topic="Fix the loader retries")
        _, text = self.details(s)
        self.assertEqual(text[:2], [" loader v2", " Fix the loader retries"])
        self.assertIn("you, with /rename", "\n".join(text))
        s = session(agent_title="Loader retries", topic="loader retries")
        _, text = self.details(s)
        self.assertEqual(text[:2], [" Loader retries", ""])
        self.assertIn("the agent", "\n".join(text))

    def test_window_title_is_the_agents_title(self):
        self.assertEqual(rt.display_title(session(agent_title="loader v2", topic="t")), "loader v2")
        self.assertEqual(rt.display_title(session(topic="t")), "t")
        self.assertEqual(rt.display_title(session(first_user_prompt="p")), "p")

    def test_lines_fit_and_long_text_is_capped(self):
        s = session(first_user_prompt="word " * 400, last_user_prompt="x")
        lines, text = self.details(s, 80)
        self.assertTrue(all(len(t) <= 80 for t in text), max(text, key=len))
        start = next(i for i, t in enumerate(text) if t.startswith("   first"))
        first = [text[start]] + [t for t in text[start + 1:start + 10]
                                 if t.startswith(" " * 16)][:9]
        self.assertEqual(len(first), 6)  # capped at six lines
        self.assertTrue(first[-1].endswith("…"))

    def test_long_labels_keep_a_gap_and_fit(self):
        s = session(topic="t")
        s.search_matches = [
            {"timestamp": "2026-10-27T10:00", "role": "assistant", "snippet": "hit " * 60},
            {"timestamp": "2026-10-27T10:01", "role": "agent_message", "snippet": "codex hit " * 30},
            {"timestamp": "2026-10-27T10:02", "role": "some_very_long_role_name", "snippet": "x"},
        ]
        _, text = self.details(s, 100)
        hits = [t for t in text if t.startswith("   [")]
        self.assertTrue(any(t.startswith("   [1] assistant 2026") for t in hits), hits)
        self.assertTrue(any(t.startswith("   [2] agent     2026") for t in hits), hits)
        self.assertTrue(all(len(t) <= 100 for t in text), max(text, key=len))
        # column 16 is the gap between the 13-character label and the value
        self.assertTrue(all(t[16] == " " for t in hits), hits)

    def test_empty_fields_are_left_out(self):
        _, text = self.details(session())
        joined = "\n".join(text)
        self.assertNotIn("git remote", joined)
        self.assertNotIn(" Summary", text)
        self.assertNotIn("Search hits", joined)

    def test_roles_are_known(self):
        s = session(topic="t", summary_error="boom")
        s.search_matches = [{"timestamp": "2026-10-27T10:00", "role": "user", "snippet": "hit"}]
        lines, _ = self.details(s)
        roles = {r for line in lines for _, r in line}
        self.assertTrue(roles <= set(termui.ROLE_COLORS) | {"", "bold"}, roles)


class TargetGroups(unittest.TestCase):
    def test_mixed_snapshot_goes_back_to_each_terminal(self):
        ss = [session("g1", terminal="ghostty"), session("t1", terminal="terminal"),
              session("g2", terminal="ghostty"), session("h1")]
        with mock.patch.object(rt, "IS_MACOS", True), \
             mock.patch.object(rt, "_current_terminal", return_value="terminal"):
            groups = rt._target_groups("auto", ss)
        self.assertEqual([(t, [s.session_id for s in g]) for t, g in groups],
                         [("ghostty", ["g1", "g2"]), ("terminal", ["t1", "h1"])])

    def test_explicit_target_and_linux_take_everything(self):
        ss = [session("g1", terminal="ghostty"), session("t1", terminal="terminal")]
        self.assertEqual([t for t, _ in rt._target_groups("byobu", ss)], ["byobu"])
        with mock.patch.object(rt, "IS_MACOS", False):
            self.assertEqual([t for t, _ in rt._target_groups("auto", ss)], ["terminator"])

    def test_ghostty_layouts_apply_to_the_ghostty_group_only(self):
        args = types.SimpleNamespace(layout="saved")
        self.assertEqual(rt._layout_for(args, "ghostty"), "saved")
        self.assertEqual(rt._layout_for(args, "terminal"), "hybrid")
        args.layout = None
        self.assertEqual(rt._layout_for(args, "ghostty"), "windows")
        self.assertEqual(rt._layout_for(args, "byobu"), "hybrid")


class EarlyLayoutCheck(unittest.TestCase):
    """A layout no session can use fails before summaries, PR lookups and the picker."""

    def run_main(self, argv, home):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["restore", *argv]), \
             mock.patch.multiple(rt, CLAUDE_ROOT=home / "projects", CODEX_ROOT=home / "codex",
                                 SNAPSHOTS_DIR=home / "snapshots", IS_MACOS=False), \
             mock.patch.object(rt, "enrich_sessions") as enrich, \
             mock.patch.object(rt, "tui_pick") as picker, \
             mock.patch.object(sys, "stderr", err), mock.patch.object(sys, "stdout", io.StringIO()):
            with self.assertRaises(SystemExit) as stop:
                rt.main()
        return stop.exception.code, enrich, picker, err.getvalue()

    def test_history_path_fails_before_enrichment(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(os.path.realpath(d))
            work = home / "w"
            work.mkdir()
            proj = home / "projects" / rt._encode_cwd_to_project(str(work))
            proj.mkdir(parents=True)
            now = datetime.now(timezone.utc)
            with (proj / "s1.jsonl").open("w") as f:
                for i in range(12):
                    ts = (now - timedelta(minutes=60 - i * 5)).isoformat()
                    role = "user" if i % 2 == 0 else "assistant"
                    content = f"prompt {i}" if role == "user" else [{"type": "text", "text": "ok"}]
                    f.write(json.dumps({"type": role, "timestamp": ts, "cwd": str(work),
                                        "message": {"role": role, "content": content}}) + "\n")
            code, enrich, picker, err = self.run_main(["--layout=saved", "--no-gh"], home)
        self.assertEqual(code, 2)
        self.assertIn("--layout=saved needs --target=ghostty", err)
        enrich.assert_not_called()
        picker.assert_not_called()


class SkipPermissions(unittest.TestCase):
    """--dangerously-skip-permissions resumes each agent with its own option for it."""

    def test_off_by_default(self):
        self.assertEqual(rt.resume_cmd(session(sid="abc")), "claude --resume abc")
        s = session(sid="r1")
        s.tool, s.path = "codex", Path("/x/r 1.jsonl")
        self.assertEqual(rt.resume_cmd(s), "codex resume '/x/r 1.jsonl'")

    def test_each_agent_gets_its_own_option(self):
        c = session(sid="abc", skip_permissions=True)
        self.assertEqual(rt.resume_cmd(c), "claude --resume abc --dangerously-skip-permissions")
        x = session(sid="r1", skip_permissions=True)
        x.tool, x.path = "codex", Path("/x/r 1.jsonl")
        # before the rollout path, where an option always parses
        self.assertEqual(rt.resume_cmd(x),
                         "codex resume --dangerously-bypass-approvals-and-sandbox '/x/r 1.jsonl'")

    def test_every_launcher_carries_it(self):
        s = session(sid="abc", cwd="/opt/a", skip_permissions=True)
        self.assertIn("claude --resume abc --dangerously-skip-permissions",
                      rt.terminator_argv(s, "t")[-1])
        self.assertTrue(rt._shell_line(s).endswith("--dangerously-skip-permissions"))

    def run_main(self, argv, home):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["restore", *argv]), \
             mock.patch.multiple(rt, CLAUDE_ROOT=home / "projects", CODEX_ROOT=home / "codex",
                                 SNAPSHOTS_DIR=home / "snapshots", IS_MACOS=False), \
             mock.patch.object(rt, "enrich_sessions"), \
             mock.patch.object(sys, "stdout", out):
            self.assertEqual(rt.main(), 0)
        return out.getvalue()

    def test_the_plan_shows_it(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(os.path.realpath(d))
            work = home / "w"
            work.mkdir()
            proj = home / "projects" / rt._encode_cwd_to_project(str(work))
            proj.mkdir(parents=True)
            now = datetime.now(timezone.utc)
            with (proj / "s1.jsonl").open("w") as f:
                for i in range(12):
                    ts = (now - timedelta(minutes=60 - i * 5)).isoformat()
                    role = "user" if i % 2 == 0 else "assistant"
                    content = f"prompt {i}" if role == "user" else [{"type": "text", "text": "ok"}]
                    f.write(json.dumps({"type": role, "timestamp": ts, "cwd": str(work),
                                        "message": {"role": role, "content": content}}) + "\n")
            plain = self.run_main(["--no-pick", "--no-gh", "--no-ai"], home)
            skip = self.run_main(["--no-pick", "--no-gh", "--no-ai",
                                  "--dangerously-skip-permissions"], home)
        self.assertIn("claude --resume s1", plain)
        self.assertNotIn("dangerously", plain)
        self.assertIn("claude --resume s1 --dangerously-skip-permissions", skip)
        self.assertIn("permission checks off", skip)


class AeroSpaceWorkspaces(unittest.TestCase):
    """--save records each window's AeroSpace workspace; --load puts windows back on it."""

    def test_ghostty_list_carries_terminal_and_window_names(self):
        us, rs = rt._US, rt._RS
        out = rs.join([us.join(["W1", "2", "/a", "✳ t", "T9", "◐ win"])]) + rs
        with mock.patch.object(rt, "_osascript", return_value=out):
            [t] = rt._ghostty_terminals()
        self.assertEqual((t["tab_index"], t["terminal_id"], t["window_name"]), (2, "T9", "◐ win"))

    def test_windows_match_by_title_without_the_status_symbol(self):
        entries = [{"terminal": "ghostty", "window_name": "◐ dotfiles"},
                   {"terminal": "ghostty", "window_name": "✳ twin"},
                   {"terminal": "ghostty", "window_name": "✳ elsewhere"},
                   {"terminal": "terminal", "window_name": "✳ dotfiles"}]
        rt._match_aerospace(entries, {"ghostty": [
            {"id": 1, "workspace": "19", "title": "✳ dotfiles"},     # the symbol animates
            {"id": 2, "workspace": "3", "title": "✳ twin"},
            {"id": 3, "workspace": "4", "title": "◑ twin"},           # same title twice: unused
        ]})
        self.assertEqual([e.get("aerospace_workspace") for e in entries], ["19", None, None, None])

    def test_snapshot_keeps_the_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.jsonl"
            p.write_text("")
            with mock.patch.object(rt, "parse_claude", return_value=session("s1")):
                [s] = rt.snapshot_to_sessions([{"tool": "claude", "jsonl_path": str(p),
                                                "aerospace_workspace": "13"}])
        self.assertEqual(s.aerospace_workspace, "13")

    def launch(self, sessions, layout, lists):
        """launch_ghostty with AeroSpace answering from lists, one per call."""
        runs, moves = [], []

        def run(script, argv):
            runs.append(argv)
            return len(argv) // 3, None
        answers = iter(lists)
        with mock.patch.object(rt, "_osascript_progress", side_effect=run), \
             mock.patch.object(rt, "_aerospace_windows", side_effect=lambda t: next(answers)), \
             mock.patch.object(rt, "_aerospace_move", side_effect=lambda i, ws: moves.append((i, ws)) or True), \
             mock.patch.object(rt.time, "sleep"), mock.patch.object(sys, "stdout", io.StringIO()) as out:
            n = rt.launch_ghostty(sessions, layout=layout, max_splits=4)
        return n, runs, moves, out.getvalue()

    def test_each_window_and_its_tabs_go_to_the_workspace(self):
        a = session("a", terminal="ghostty", window_id="W1", tab_index=1, aerospace_workspace="13")
        b = session("b", terminal="ghostty", window_id="W1", tab_index=2, aerospace_workspace="13")
        c = session("c", terminal="ghostty", window_id="W2", tab_index=1, aerospace_workspace="19")
        old = {"id": 1, "workspace": "1", "title": "x"}
        tab1, tab2, other = ({"id": i, "workspace": "1", "title": "y"} for i in (10, 11, 12))
        n, runs, moves, out = self.launch([a, b, c], "saved", [
            [old],                        # before anything opens
            [old, tab1], [old, tab1, tab2],   # the first window shows up, then its tab
            [old, tab1, tab2, other],     # the second window
        ])
        self.assertEqual(n, 3)
        self.assertEqual([r[0::3] for r in runs], [["window", "tab"], ["window"]])
        self.assertEqual(moves, [(10, "13"), (11, "13"), (12, "19")])
        self.assertIn("→ workspace 13", out)

    def test_without_workspaces_everything_opens_in_one_run(self):
        with mock.patch.object(rt, "_aerospace_windows") as aero:
            n, runs, moves, _ = self.launch([session("a"), session("b")], "windows", [])
        self.assertEqual((n, len(runs), moves), (2, 1, []))
        aero.assert_not_called()


class Restart(unittest.TestCase):
    """--restart ends running sessions and resumes each in the terminal it ran in."""

    def test_inside_an_agent(self):
        procs = {1: proc(1, 0, "launchd"), 10: proc(10, 1, "zsh"), 20: proc(20, 10, "claude"),
                 30: proc(30, 20, "bash -c python3"), 40: proc(40, 30, "python3 restore")}
        with mock.patch.object(rt, "_claude_pid_record_pids", return_value=[]):
            self.assertTrue(rt._inside_agent(procs, 40))
            self.assertFalse(rt._inside_agent(procs, 10))
            self.assertFalse(rt._inside_agent(procs, 20))   # the agent itself is not inside one

    def test_what_can_be_restarted_in_place(self):
        self.assertTrue(rt._restartable({"terminal": "ghostty", "terminal_id": "T1"}))
        self.assertFalse(rt._restartable({"terminal": "ghostty"}))
        self.assertTrue(rt._restartable({"terminal": "terminal", "window_id": "5", "tab_index": 2}))
        self.assertFalse(rt._restartable({"terminal": "tmux"}))
        self.assertFalse(rt._restartable({"terminal": None}))

    def entry(self, sid, pid, **kw):
        return {"tool": "claude", "session_id": sid, "pid": pid, "cwd": "/w/a",
                "jsonl_path": f"/x/{sid}.jsonl", **kw}

    def run_restart(self, argv, entries, answer="y", tty=True):
        sessions = [session(e["session_id"]) for e in entries if rt._restartable(e)]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["restore", "--restart", "--no-gh", "--no-ai", *argv]), \
             mock.patch.object(rt, "IS_MACOS", True), \
             mock.patch.object(rt, "_inside_agent", return_value=False), \
             mock.patch.object(rt, "snapshot_active", return_value=entries), \
             mock.patch.object(rt, "snapshot_to_sessions", return_value=sessions), \
             mock.patch.object(rt, "enrich_sessions"), \
             mock.patch.object(rt, "save_snapshot", return_value=Path("/s.json")) as save, \
             mock.patch.object(rt, "restart_in_place", side_effect=lambda pairs: len(pairs)) as again, \
             mock.patch.object(sys.stdin, "isatty", return_value=tty), \
             mock.patch("builtins.input", return_value=answer), \
             mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = rt.main()
        return code, out.getvalue() + err.getvalue(), save, again

    def test_dry_run_ends_nothing(self):
        entries = [self.entry("a", 11, terminal="ghostty", terminal_id="T1", aerospace_workspace="13"),
                   self.entry("b", 12, terminal="tmux")]
        code, out, save, again = self.run_restart(["--no-pick", "--dangerously-skip-permissions"], entries)
        self.assertEqual(code, 0)
        self.assertIn("left alone", out)
        self.assertIn("(ghostty, workspace 13)", out)
        self.assertIn("claude --resume a --dangerously-skip-permissions", out)
        save.assert_not_called()
        again.assert_not_called()

    def test_execute_asks_first(self):
        entries = [self.entry("a", 11, terminal="ghostty", terminal_id="T1")]
        code, out, save, again = self.run_restart(["--no-pick", "--execute"], entries, answer="n")
        self.assertIn("nothing was ended", out)
        again.assert_not_called()
        code, out, save, again = self.run_restart(["--no-pick", "--execute"], entries, tty=False)
        self.assertEqual(code, 2)
        again.assert_not_called()
        code, out, save, again = self.run_restart(["--no-pick", "--execute"], entries, answer="y")
        self.assertEqual(code, 0)
        self.assertIn("Will restart 1 session(s)", out)
        self.assertNotIn("--dry-run", out)
        save.assert_called_once()
        [(s, e)] = again.call_args.args[0]
        self.assertEqual((s.session_id, e["terminal_id"]), ("a", "T1"))

    def test_refused_inside_an_agent_and_with_load(self):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["restore", "--restart"]), \
             mock.patch.object(rt, "IS_MACOS", True), \
             mock.patch.object(rt, "_inside_agent", return_value=True), \
             mock.patch.object(rt, "snapshot_active") as snap, mock.patch.object(sys, "stderr", err):
            self.assertEqual(rt.main(), 2)
        snap.assert_not_called()
        self.assertIn("plain terminal", err.getvalue())
        with mock.patch.object(sys, "argv", ["restore", "--restart", "--load"]), \
             mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(SystemExit):
            rt.main()

    def test_in_place_ends_types_and_checks(self):
        s1 = session("a", cwd="/w/a", skip_permissions=True)
        s2 = session("b", cwd="/w/b")
        e1 = self.entry("a", 11, terminal="ghostty", terminal_id="T1")
        e2 = self.entry("b", 12, terminal="terminal", window_id="5", tab_index=2)
        killed, typed = [], []

        def kill(pid, sig):
            if sig == rt.signal.SIGTERM:
                killed.append(pid)
            else:
                raise ProcessLookupError
        with mock.patch.object(rt.os, "kill", side_effect=kill), \
             mock.patch.object(rt, "_osascript", side_effect=lambda script, *a: typed.append(a) or "ok"), \
             mock.patch.object(rt, "snapshot_active", return_value=[{"session_id": "a"}, {"session_id": "b"}]), \
             mock.patch.object(rt.time, "sleep"), mock.patch.object(sys, "stdout", io.StringIO()):
            back = rt.restart_in_place([(s1, e1), (s2, e2)])
        self.assertEqual(back, 2)
        self.assertEqual(killed, [11, 12])
        self.assertEqual(typed, [("T1", "cd -- /w/a && claude --resume a --dangerously-skip-permissions"),
                                 ("5", "2", "cd -- /w/b && claude --resume b")])

    def test_already_without_checks_is_left_alone(self):
        entries = [self.entry("a", 11, terminal="ghostty", terminal_id="T1", skip_permissions=True),
                   self.entry("b", 12, terminal="ghostty", terminal_id="T2")]
        code, out, _, _ = self.run_restart(["--no-pick", "--dangerously-skip-permissions"], entries)
        self.assertEqual(code, 0)
        self.assertIn("already without permission checks", out)
        self.assertIn("Would restart 1 session(s)", out)
        self.assertIn("claude --resume b --dangerously-skip-permissions", out)
        self.assertNotIn("claude --resume a", out)
        # without the option every running session can be picked
        code, out, _, _ = self.run_restart(["--no-pick"], entries)
        self.assertIn("Would restart 2 session(s)", out)
        self.assertIn("checks: off now, on after the restart", out)

    def test_a_session_that_does_not_exit_is_left_running(self):
        s1 = session("a")
        e1 = self.entry("a", 11, terminal="ghostty", terminal_id="T1")
        with mock.patch.object(rt.os, "kill"), \
             mock.patch.object(rt, "_wait_exit", return_value={11}), \
             mock.patch.object(rt, "_osascript") as osa, \
             mock.patch.object(rt, "snapshot_active", return_value=[]), \
             mock.patch.object(rt.time, "sleep"), mock.patch.object(sys, "stdout", io.StringIO()) as out:
            back = rt.restart_in_place([(s1, e1)])
        self.assertEqual(back, 0)
        osa.assert_not_called()
        self.assertIn("did not exit", out.getvalue())


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


class RunningSessions(unittest.TestCase):
    """What restore treats as already running, and leaves out of the picker."""

    def test_recorded_process_counts_whatever_its_name(self):
        with tempfile.TemporaryDirectory() as d:
            pid_dir = Path(d)
            (pid_dir / "50.json").write_text(json.dumps({"pid": 50, "sessionId": "forked"}))
            procs = {50: proc(50, 1, "/x/claude/versions/2.1.312 --session-id forked", uid=os.getuid()),
                     51: proc(51, 1, "claude agents", uid=os.getuid())}
            with mock.patch.multiple(rt, CLAUDE_PID_DIR=pid_dir, CLAUDE_ROOT=pid_dir / "none"), \
                    mock.patch.object(rt, "_list_procs", return_value=procs), \
                    mock.patch.object(rt, "_proc_cwds", return_value={50: "/w/a", 51: "/w/a"}):
                found = rt.snapshot_active(with_windows=False)
        self.assertEqual([(e["pid"], e["session_id"]) for e in found], [(50, "forked")])

    def test_left_behind_record_does_not_make_a_session(self):
        with tempfile.TemporaryDirectory() as d:
            pid_dir = Path(d)
            (pid_dir / "60.json").write_text(json.dumps(
                {"pid": 60, "sessionId": "gone", "startedAt": 1_000_000_000_000}))
            procs = {60: proc(60, 1, "/usr/bin/python3 server.py --session-id x", uid=os.getuid())}
            procs[60].start = 1_800_000_000.0   # started long after the record
            with mock.patch.multiple(rt, CLAUDE_PID_DIR=pid_dir, CLAUDE_ROOT=pid_dir / "none"), \
                    mock.patch.object(rt, "_list_procs", return_value=procs), \
                    mock.patch.object(rt, "_proc_cwds", return_value={60: "/w/a"}):
                self.assertEqual(rt.snapshot_active(with_windows=False), [])

    def test_running_sessions_are_left_out(self):
        ss = [session("a"), session("b"), session("c")]
        with mock.patch.object(rt, "snapshot_active", return_value=[{"session_id": "b"}]), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            kept = rt.drop_running(ss)
        self.assertEqual([s.session_id for s in kept], ["a", "c"])
        self.assertIn("1", out.getvalue())
        self.assertIn("already-running", out.getvalue())

    def test_running_codex_session_is_known_by_its_rollout_file(self):
        now = datetime.now(timezone.utc)
        rollout = "rollout-2026-10-28T09-00-00-0199aaaa"
        s = rt.Session(tool="codex", session_id="0199aaaa", path=Path(f"/c/{rollout}.jsonl"),
                       cwd="/w/a", first_ts=now, last_ts=now)
        with mock.patch.object(rt, "snapshot_active", return_value=[{"session_id": rollout}]), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(rt.drop_running([s]), [])

    def test_picker_shows_running_sessions_marked(self):
        ss = [session("a"), session("b")]
        shown = []
        def pick(items):   # returns None: cancelled, so nothing launches
            shown.extend((s.session_id, bool(s.live)) for s in items)
        with mock.patch.object(rt, "discover", return_value=ss), \
                mock.patch.object(rt, "apply_filters", side_effect=lambda found, **kw: found), \
                mock.patch.object(rt, "snapshot_active", return_value=[{"session_id": "a"}]), \
                mock.patch.object(rt, "enrich_sessions"), \
                mock.patch.object(rt, "tui_pick", side_effect=pick), \
                mock.patch.object(rt, "print_stats"), \
                tempfile.TemporaryDirectory() as d, \
                mock.patch.object(sys, "argv", ["restore", "--no-ai", "--no-gh",
                                                "--cache", f"{d}/cache.json"]), \
                mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch.object(out, "isatty", return_value=True, create=True):
            rt.main()
        self.assertEqual(sorted(shown), [("a", True), ("b", False)])
        self.assertIn("of these run now", out.getvalue())


class SnapshotLiveProcess(unittest.TestCase):
    """Starts a real process named `claude` and finds it, through /proc on
    Linux and ps/lsof on macOS."""

    def test_finds_fake_claude_process(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(os.path.realpath(d))
            work = base / "work"
            work.mkdir()
            spawned = time.time()
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
                        start = rt._list_procs()[child.pid].start
                        if found:
                            entry = found[0]
                            break
                        time.sleep(0.1)
            finally:
                child.kill()
                child.wait()
        self.assertIsNotNone(entry, "fake claude process not found")
        self.assertAlmostEqual(start, spawned, delta=5)
        self.assertEqual(entry["session_id"], "fake-sid")
        self.assertEqual(entry["cwd"], str(work))
        self.assertEqual(entry["jsonl_path"], str(projects / "fake-sid.jsonl"))


class RunningInPicker(unittest.TestCase):
    """The picker lists running sessions, says what they run with, and
    restarts the ones picked in place."""

    def test_permission_checks_read_from_the_command_line(self):
        on = rt.runs_without_checks
        self.assertTrue(on("claude", ["claude", "--resume", "x", "--dangerously-skip-permissions"]))
        self.assertTrue(on("claude", ["claude", "--permission-mode", "bypassPermissions"]))
        self.assertTrue(on("claude", ["claude", "--permission-mode=bypassPermissions"]))
        self.assertFalse(on("claude", ["claude", "--permission-mode", "plan"]))
        self.assertFalse(on("claude", ["claude", "--resume", "x"]))
        self.assertFalse(on("claude", ["claude", "--allow-dangerously-skip-permissions"]))
        self.assertTrue(on("codex", ["codex", "resume", "--dangerously-bypass-approvals-and-sandbox", "/r"]))
        self.assertTrue(on("codex", ["codex", "--yolo"]))
        self.assertFalse(on("codex", ["codex", "resume", "/r"]))

    def live(self, s, skip_now=False, **kw):
        s.live = {"pid": 7, "terminal": "ghostty", "terminal_id": "T1",
                  "skip_permissions": skip_now, **kw}
        return s

    def test_badge_flags_other_settings(self):
        self.assertEqual(rt._live_badge(session("a")), [])
        self.assertEqual(rt._live_badge(self.live(session("a"))), [("● running", "bold")])
        s = self.live(session("b", skip_permissions=True))
        self.assertEqual(rt._live_badge(s), [("● running, checks on", "warn")])
        for run_off in (False, True):   # checks off is marked whatever this run does
            s = self.live(session("c", skip_permissions=run_off), skip_now=True)
            self.assertEqual(rt._live_badge(s), [("●", "alert"), (" running, checks off", "warn")])
        self.assertIn("restarts it in place with them off",
                      rt._live_note(self.live(session("d", skip_permissions=True))))
        self.assertIn("cannot type into", rt._live_note(self.live(session("e"), terminal="tmux")))

    def test_running_rows_never_look_ticked_and_red_is_only_a_symbol(self):
        rows = [self.live(session("a")), self.live(session("b", skip_permissions=True)),
                self.live(session("c"), skip_now=True)]
        for s in rows:
            segs = rt._live_badge(s)
            self.assertNotIn("ok", [r for _, r in segs])   # green means ticked
            self.assertTrue(all(t == "●" for t, r in segs if r == "alert"))

    def test_running_column_keeps_rows_aligned(self):
        rows = [session("a"), self.live(session("b")), self.live(session("c", skip_permissions=True)),
                self.live(session("d"), skip_now=True)]
        w = max(rt._badge_len(s) for s in rows)
        cwd_at = ["".join(t for t, _ in rt._row_segments(s, 120, w)).index(" /w/a") for s in rows]
        self.assertEqual(len(set(cwd_at)), 1)
        self.assertNotIn("running", rt._row_text(rows[1], 120))   # no column without live_w

    def test_details_and_footer_say_what_picking_does(self):
        s = self.live(session("a", skip_permissions=True))
        text = "\n".join("".join(t for t, _ in line) for line in rt._detail_lines(s, 100))
        self.assertIn("Running now", text)
        self.assertIn("on now; this run resumes with them off", text)
        loc, _ = rt._picker_footer(s)
        self.assertIn("picking it restarts it in place", loc[0][0])

    def test_picked_running_sessions_restart_and_the_rest_open(self):
        new, running = session("a"), self.live(session("b"))
        args = types.SimpleNamespace()
        with mock.patch.object(rt, "_plan_and_launch") as launch, \
             mock.patch.object(rt, "_restart_picked", return_value=0) as again:
            self.assertEqual(rt._launch_and_restart(None, args, [new, running]), 0)
        self.assertEqual(launch.call_args.args[2], [new])
        self.assertEqual(again.call_args.args[1], [running])
        with mock.patch.object(rt, "_plan_and_launch") as launch, \
             mock.patch.object(rt, "_restart_picked", return_value=0):
            rt._launch_and_restart(None, args, [running])
        launch.assert_not_called()

    def test_restart_picked(self):
        args = types.SimpleNamespace(execute=False)
        tmux = self.live(session("t"), terminal="tmux")
        s = self.live(session("a", skip_permissions=True))
        with mock.patch.object(sys, "stdout", io.StringIO()) as out:
            self.assertEqual(rt._restart_picked(args, [tmux, s]), 0)
        self.assertIn("left running", out.getvalue())
        self.assertIn("Would restart 1 session(s)", out.getvalue())
        self.assertIn("checks: on now, off after the restart", out.getvalue())
        # with --execute, never from inside an agent: it would end restore too
        args.execute = True
        with mock.patch.object(rt, "_inside_agent", return_value=True), \
             mock.patch.object(rt, "restart_in_place") as again, \
             mock.patch.object(sys, "stdout", io.StringIO()), \
             mock.patch.object(sys, "stderr", io.StringIO()) as err:
            self.assertEqual(rt._restart_picked(args, [s]), 2)
        again.assert_not_called()
        self.assertIn("plain terminal", err.getvalue())

    def test_a_running_session_the_filters_dropped_is_listed(self):
        old, new, idle = session("old"), session("new"), session("idle")
        with mock.patch.object(rt, "snapshot_active", return_value=[{"session_id": "old"}]), \
             mock.patch.object(sys, "stdout", io.StringIO()) as out:
            shown = rt._running_in_list(types.SimpleNamespace(pick=True), [new], [old, new, idle])
        self.assertEqual(sorted(s.session_id for s in shown), ["new", "old"])
        self.assertIn("1 listed only because they run", out.getvalue())

    def test_without_the_picker_running_sessions_are_left_out(self):
        ss = [session("a"), session("b")]
        with mock.patch.object(rt, "snapshot_active", return_value=[{"session_id": "a"}]), \
             mock.patch.object(sys, "stdout", io.StringIO()):
            kept = rt._running_in_list(types.SimpleNamespace(pick=False), ss)
        self.assertEqual([s.session_id for s in kept], ["b"])


if __name__ == "__main__":
    unittest.main()
