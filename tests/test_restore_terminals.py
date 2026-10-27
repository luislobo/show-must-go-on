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
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("restore_terminals", ROOT / "restore_terminals.py")
rt = importlib.util.module_from_spec(_spec)
sys.modules["restore_terminals"] = rt  # dataclasses look the module up while building Session
_spec.loader.exec_module(rt)


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
