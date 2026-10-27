"""Tests for the platform layer of restore_terminals.py: finding running
sessions and the terminals they run in, and reopening them.

Stdlib only. Run from the repo root:  python3 -m unittest discover tests
"""

import importlib.util
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
                projects = base / "projects" / rt._encode_cwd_to_project(str(work))
                projects.mkdir(parents=True)
                (projects / "fake-sid.jsonl").write_text("{}\n")
                with mock.patch.object(rt, "CLAUDE_ROOT", base / "projects"):
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
