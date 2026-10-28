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
