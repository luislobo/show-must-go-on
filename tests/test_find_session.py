"""Tests for find_session.py's output: colours on a terminal, plain when piped.

Stdlib only. Run from the repo root:  python3 -m unittest discover tests
"""

import importlib.util
import io
import re
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import termui  # noqa: E402

_spec = importlib.util.spec_from_file_location("find_session", ROOT / "find_session.py")
fs = importlib.util.module_from_spec(_spec)
sys.modules["find_session"] = fs  # dataclasses look the module up while building SessionHit
_spec.loader.exec_module(fs)


def hit():
    now = datetime.now(timezone.utc)
    return fs.SessionHit(
        tool="claude", session_id="abc", path=Path("/x/abc.jsonl"), cwd="/w/a",
        first_ts=now, last_ts=now, git={"branch": "fix/retries"},
        matches=[fs.Match(timestamp="2026-10-27T10:00", role="user",
                          snippet="Use exponential Backoff, then backoff again")])


class Output(unittest.TestCase):
    def render(self, colours: bool) -> str:
        out = io.StringIO()
        with mock.patch.object(termui, "stdout_colors", return_value=colours), \
             mock.patch.object(sys, "stdout", out):
            fs.print_hits([hit()], "backoff", 5, re.compile("backoff", re.IGNORECASE))
        return out.getvalue()

    def test_colours_on_a_terminal(self):
        text = self.render(True)
        self.assertIn("\033[33mBackoff\033[0m", text)       # every match, any case
        self.assertIn("\033[33mbackoff\033[0m", text)
        self.assertIn("\033[32mfix/retries\033[0m", text)
        self.assertIn("\033[1mclaude --resume abc\033[0m", text)
        self.assertIn("\033[32m[user]\033[0m", text)

    def test_plain_when_piped(self):
        text = self.render(False)
        self.assertNotIn("\033[", text)
        self.assertIn("Use exponential Backoff, then backoff again", text)
        self.assertIn("resume  : claude --resume abc", text)

    def test_no_match(self):
        out = io.StringIO()
        with mock.patch.object(termui, "stdout_colors", return_value=True), \
             mock.patch.object(sys, "stdout", out):
            fs.print_hits([], "nothing", 5)
        self.assertIn("\033[33mNo sessions match 'nothing'.\033[0m", out.getvalue())

    def test_starts_in_isolated_mode(self):
        r = subprocess.run([sys.executable, "-I", str(ROOT / "find_session.py"), "--help"],
                           capture_output=True, text=True, cwd="/")
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
