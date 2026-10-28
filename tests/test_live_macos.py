"""Live tests against the real Terminal.app and Ghostty on macOS.

They open and close windows, so they run only when asked:

    SMGO_LIVE=1 python3 -m unittest tests.test_live_macos

Each test closes exactly the windows it opened, and quits Terminal.app
again if it was not running before. Ghostty needs an unlocked screen: it
starts a terminal's shell only once it can draw the window.
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
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location("restore_terminals", ROOT / "restore_terminals.py")
rt = importlib.util.module_from_spec(_spec)
sys.modules["restore_terminals"] = rt
_spec.loader.exec_module(rt)

LIVE = sys.platform == "darwin" and os.environ.get("SMGO_LIVE") == "1"


def osascript(script: str) -> str:
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                          timeout=30).stdout.strip()


def is_running(app: str) -> bool:
    """Asked through AppleScript, which does not launch the app (pgrep -x does
    not match Ghostty's process name)."""
    return osascript(f'application "{app}" is running') == "true"


def window_ids(app: str) -> set[str]:
    out = osascript(f'tell application "{app}" to get id of every window')
    return {w.strip() for w in out.split(",") if w.strip() and w.strip() != "missing value"}


def ghostty_windows_in(base: Path) -> set[str]:
    """Ghostty windows whose every terminal is in a folder under base. Ghostty
    gives a window a new id as tabs join it, so ids taken right after a
    launch go stale; the folders do not, and no other window can be in the
    test's own temp folder."""
    out = osascript(
        'set us to character id 31\n'
        'set out to ""\n'
        'tell application "Ghostty"\n'
        '  repeat with w in windows\n'
        '    repeat with t in terminals of w\n'
        '      set out to out & (id of w) & us & (working directory of t) & linefeed\n'
        '    end repeat\n'
        '  end repeat\n'
        'end tell\n'
        'return out')
    dirs: dict[str, list[str]] = {}
    for line in out.splitlines():
        wid, _, wd = line.partition("\x1f")
        dirs.setdefault(wid, []).append(wd)
    root = str(base)
    return {wid for wid, wds in dirs.items()
            if wds and all(wd == root or wd.startswith(root + "/") for wd in wds)}


def session(sid: str, cwd: Path) -> "rt.Session":
    now = datetime.now(timezone.utc)
    return rt.Session(tool="claude", session_id=sid, path=Path("/x"), cwd=str(cwd),
                      first_ts=now, last_ts=now, topic=f"live test {sid}")


def wait_for(check, timeout: float = 20.0):
    end = time.time() + timeout
    while time.time() < end:
        value = check()
        if value:
            return value
        time.sleep(0.3)
    return check()


class LiveBase(unittest.TestCase):
    APP = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(os.path.realpath(self.tmp.name))
        self.marker = self.base / "marker.txt"
        self.was_running = is_running(self.APP)
        self.opened: set[str] = set()   # only these windows are ever closed

    def launch(self, fn):
        """Run a launcher and record the windows it opened, by comparing the
        window list just before and after. Windows that existed before are
        never recorded, so the cleanup cannot touch them."""
        before = window_ids(self.APP) if is_running(self.APP) else set()
        result = fn()
        time.sleep(1.0)   # let new windows and tabs settle
        if self.APP == "Ghostty":
            self.opened = wait_for(lambda: ghostty_windows_in(self.base), 10) or set()
        else:
            self.opened = window_ids(self.APP) - before
        return result

    def tearDown(self):
        for pid in getattr(self, "fake_pids", []):
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        left = set(self.opened)
        for _ in range(5):
            time.sleep(0.5)
            if self.APP == "Ghostty":
                left = ghostty_windows_in(self.base)
            else:
                left &= window_ids(self.APP)
            if not left:
                break
            for wid in left:
                if self.APP == "Ghostty":
                    # Ghostty's plain `close` closes a terminal; windows need `close window`
                    osascript(f'tell application "Ghostty" to close window (window id "{wid}")')
                else:
                    osascript(f'tell application "{self.APP}" to close (window id {wid})')
        if not self.was_running and self.APP == "Terminal" and not window_ids("Terminal"):
            osascript('tell application "Terminal" to quit')
        self.tmp.cleanup()
        self.assertFalse(left, f"left {self.APP} windows open: {left}")

    def folders(self, n: int) -> list[Path]:
        dirs = []
        for i in range(n):
            d = self.base / f"folder {i}"   # a space, to exercise the quoting
            d.mkdir()
            dirs.append(d)
        return dirs

    def fake_claude(self, cwd: Path) -> "rt.Proc":
        """The process a launched `exec -a claude sleep` became, once it shows up."""
        def find():
            procs = rt._list_procs()
            cwds = rt._proc_cwds([p.pid for p in procs.values()
                                  if p.argv and p.argv[0] == "claude"])
            for pid, d in cwds.items():
                if d == str(cwd):
                    return procs[pid]
            return None
        proc = wait_for(find)
        self.assertIsNotNone(proc, "the fake claude process never started")
        self.fake_pids = [proc.pid]
        return proc

    def save_finds(self, proc: "rt.Proc", cwd: Path) -> dict:
        pid_dir = self.base / "pid-records"
        pid_dir.mkdir()
        (pid_dir / f"{proc.pid}.json").write_text(json.dumps(
            {"pid": proc.pid, "sessionId": "live-test", "cwd": str(cwd),
             "startedAt": (proc.start or time.time()) * 1000}))
        with mock.patch.multiple(rt, CLAUDE_PID_DIR=pid_dir, CLAUDE_ROOT=self.base / "projects"):
            entries = [e for e in rt.snapshot_active() if e["pid"] == proc.pid]
        self.assertEqual(len(entries), 1, "--save did not find the fake session")
        return entries[0]


@unittest.skipUnless(LIVE, "live macOS test: set SMGO_LIVE=1")
class TerminalAppLive(LiveBase):
    APP = "Terminal"

    def test_launch_runs_in_each_folder(self):
        dirs = self.folders(2)
        with mock.patch.object(rt, "resume_cmd", lambda s: f"pwd >> '{self.marker}'"):
            n = self.launch(lambda: rt.launch_terminal_app(
                [session(f"s{i}", d) for i, d in enumerate(dirs)]))
        self.assertEqual(n, 2)
        self.assertEqual(len(self.opened), 2)
        lines = wait_for(lambda: self.marker.exists() and len(self.marker.read_text().splitlines()) >= 2
                         and self.marker.read_text().splitlines())
        self.assertEqual(sorted(lines), sorted(str(d) for d in dirs))

    def test_save_records_the_window_and_tab(self):
        [d] = self.folders(1)
        with mock.patch.object(rt, "resume_cmd", lambda s: "exec -a claude sleep 60"):
            self.launch(lambda: rt.launch_terminal_app([session("s0", d)]))
        proc = self.fake_claude(d)
        entry = self.save_finds(proc, d)
        self.assertEqual(entry["terminal"], "terminal")
        self.assertIn(str(entry.get("window_id")), self.opened)
        self.assertEqual(entry.get("tab_index"), 1)


@unittest.skipUnless(LIVE, "live macOS test: set SMGO_LIVE=1")
class GhosttyLive(LiveBase):
    APP = "Ghostty"

    def test_launch_runs_in_each_folder(self):
        dirs = self.folders(3)
        with mock.patch.object(rt, "resume_cmd", lambda s: f"pwd >> '{self.marker}'"):
            n = self.launch(lambda: rt.launch_ghostty(
                [session(f"s{i}", d) for i, d in enumerate(dirs)], layout="tabs", max_splits=4))
        self.assertEqual(n, 3)
        self.assertEqual(len(self.opened), 1, "tabs layout should open one window")
        lines = wait_for(lambda: self.marker.exists() and len(self.marker.read_text().splitlines()) >= 3
                         and self.marker.read_text().splitlines())
        self.assertEqual(sorted(lines or []), sorted(str(d) for d in dirs),
                         "no shell ran: is the screen locked?")

    def test_save_records_the_window(self):
        [d] = self.folders(1)
        with mock.patch.object(rt, "resume_cmd", lambda s: "exec -a claude sleep 60"):
            self.launch(lambda: rt.launch_ghostty([session("s0", d)], layout="windows", max_splits=4))
        proc = self.fake_claude(d)
        entry = self.save_finds(proc, d)
        self.assertEqual(entry["terminal"], "ghostty")
        self.assertIn(entry.get("window_id"), self.opened)


if __name__ == "__main__":
    unittest.main()
