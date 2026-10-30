"""Record the demo as an asciicast v2 file by driving a shell in a pty.

    python3 demo/record.py --lines > lines.json
    python3 demo/record.py <demo-home> <out.cast> [<narration-dir>]

Runs zsh with HOME=<demo-home> and demo/bin first on PATH (stand-ins for
`claude -p` and `gh`), types the steps in SCRIPT on a fixed timeline, and
waits for the screen to show what each step expects before moving on.
A title card and an end card are written straight into the cast.

--lines prints every narration line, with the text to speak, for
demo/voice.py to voice. Given the folder voice.py wrote, the recording
waits for each line to finish before the next one starts, so the video
always has room for the voice, and narration.json there lists each clip
with its start time in the cast.
"""

from __future__ import annotations

import codecs
import fcntl
import json
import os
import pty
import random
import select
import struct
import sys
import termios
import time
from pathlib import Path

COLS, ROWS = 140, 34
REPO = Path(__file__).resolve().parent.parent

# curses switches the terminal to application keypad mode (smkx), where the
# arrows arrive as ESC O C / ESC O D, as a real terminal would send them
RIGHT, LEFT, ENTER = "\x1bOC", "\x1bOD", "\r"

# What the narrator says over the title card and the end card
TITLE_LINE = ("show-must-go-on finds the Claude Code and Codex sessions you had open, "
              "and reopens them after a reboot.")
END_LINE = "Proudly made in Dallas."

# Words the voice reads wrongly, and how to spell them for it
SPOKEN = {"show-must-go-on": "show must go on", "tmux": "tee mux", "Codex": "codex",
          "--execute": "execute", "--find": "find", "Ghostty": "Ghosty",
          "summarises": "summarizes"}

# (action, argument, pause after in seconds). A `say` step starts a narration
# line; the next one waits until it has been spoken.
SCRIPT = [
    ("wait_for", "$ ", 0.8),
    ("type", "restore", 0.3),
    ("say", "restore reads every session on disk, and summarises the recent ones.", 0.0),
    ("key", ENTER, 0.0),
    ("wait_for", "selected", 0.6),     # the picker is up
    ("say", ("The picker lists them, newest first: the folder, the branch and its pull "
             "request, then the agent's own title in bold, with restore's summary beside it."), 1.6),
    ("key", "j", 0.7),
    ("key", " ", 0.8),                 # tick the second session
    ("key", "j", 0.6),
    ("key", "j", 0.6),
    ("key", " ", 0.9),                 # tick the fourth
    ("key", "k", 0.5),
    ("key", "k", 0.5),
    ("key", "k", 0.7),
    ("say", ("The right arrow opens a session's details: who named it, where it ran, "
             "a summary, and the last hour of the conversation."), 0.0),
    ("key", RIGHT, 3.2),               # details of the first session
    ("key", "j", 0.35), ("key", "j", 0.35), ("key", "j", 0.35),
    ("key", "j", 0.35), ("key", "j", 0.35), ("key", "j", 2.0),
    ("key", LEFT, 1.2),
    ("say", ("Enter shows the plan. With --execute, restore opens each session in Ghostty, "
             "Terminal, Terminator or tmux, and skips any that are already running."), 0.0),
    ("key", ENTER, 0.0),
    ("wait_for", "dry-run", 3.5),
    ("say", ("restore --find searches every transcript, here for backoff, "
             "and ranks the sessions that match."), 0.0),
    ("key", "\x0c", 0.8),              # Ctrl-L: zsh clears the screen
    ("type", "restore --find backoff", 0.3),
    ("key", ENTER, 0.0),
    ("wait_for", "selected", 2.2),
    ("say", "Its details show the context of each match.", 0.0),
    ("key", RIGHT, 1.6),
    *[("key", "j", 0.3)] * 7,          # scroll down to the search hits
    ("key", "j", 3.0),
    ("key", LEFT, 1.0),
    ("key", "q", 0.0),
    ("wait_for", "(cancelled)", 1.5),
]

BOLD, CYAN, RED, RESET = "\x1b[1m", "\x1b[36m", "\x1b[31m", "\x1b[0m"
HIDE_CURSOR, SHOW_CURSOR = "\x1b[?25l", "\x1b[?25h"
TITLE_CARD = (
    HIDE_CURSOR + "\x1b[2J\x1b[H" + "\r\n" * 13 +
    f"          {BOLD}show-must-go-on{RESET}\r\n\r\n"
    "          Finds the Claude Code and Codex sessions you had open,\r\n"
    "          and reopens them after a reboot.\r\n\r\n"
    f"          {CYAN}Linux{RESET}  i3 and Terminator     "
    f"{CYAN}macOS{RESET}  Ghostty and Terminal.app     {CYAN}Anywhere{RESET}  tmux\r\n"
)
END_CARD = (
    HIDE_CURSOR + "\x1b[2J\x1b[H" + "\r\n" * 14 +
    f"          {BOLD}show-must-go-on{RESET}\r\n\r\n"
    f"          github.com/luislobo/show-must-go-on\r\n\r\n"
    f"          Proudly made in Dallas with {RED}♥{RESET}\r\n"
)


class Narrator:
    """Places the voiced lines on the recording and keeps it from running ahead of them."""

    def __init__(self, folder: Path | None):
        self.folder = folder
        self.clips: list[dict] = []
        self.made: dict[str, dict] = {}
        if folder:
            voiced = folder / "voice.json"
            if not voiced.exists():
                raise SystemExit(f"{voiced}: missing; voice the lines first with demo/voice.py")
            self.made = {c["text"]: c for c in json.loads(voiced.read_text())["lines"]}
            missing = [t for t in narration_lines() if t not in self.made]
            if missing:
                raise SystemExit(f"{voiced}: no clip for {missing[0]!r}; voice the lines again")

    def speak(self, text: str) -> float:
        """Queue a line's clip; returns how long it plays, 0 without narration."""
        if not self.folder:
            return 0.0
        self.clips.append(dict(self.made[text]))
        return self.clips[-1]["seconds"]

    def place(self, t: float) -> None:
        self.clips[-1]["start"] = round(t, 3)

    def write(self) -> None:
        if self.folder:
            (self.folder / "narration.json").write_text(json.dumps(self.clips, indent=1))


def narration_lines() -> list[str]:
    return [TITLE_LINE, *[a for k, a, _ in SCRIPT if k == "say"], END_LINE]


def spoken(text: str) -> str:
    for word, said in SPOKEN.items():
        text = text.replace(word, said)
    return text


def main(home: Path, out: Path, narration: Path | None = None) -> None:
    env = {
        "HOME": str(home),
        "PATH": f"{REPO / 'demo' / 'bin'}:{Path.home() / '.local' / 'bin'}:/usr/bin:/bin:/opt/homebrew/bin",
        "TERM": "xterm-256color",
        "TERM_PROGRAM": "ghostty",     # restore's auto target: Ghostty
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "PS1": "%F{cyan}~/code%f $ ",
        "ESCDELAY": "25",
    }
    events: list[tuple[float, str]] = []
    t0 = 0.0
    narrator = Narrator(narration)
    gap = 0.35          # between one line and the next

    def emit(text: str, hold: float) -> None:
        nonlocal t0
        events.append((t0, text))
        t0 += hold

    title_seconds = narrator.speak(TITLE_LINE)
    if title_seconds:
        narrator.place(0.4)
    emit(TITLE_CARD, max(3.8, 0.4 + title_seconds + gap))
    emit(SHOW_CURSOR + "\x1b[2J\x1b[H", 0.0)

    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(sys.stdout.fileno(), termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        os.chdir(home / "code")
        os.execve("/bin/zsh", ["zsh", "-f", "-i"], env)

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    start = time.monotonic()
    screen_since_mark = ""

    def pump(seconds: float) -> None:
        """Read output for `seconds`, recording each chunk with its time."""
        nonlocal screen_since_mark
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            r, _, _ = select.select([fd], [], [], min(left, 0.05))
            if r:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    return
                if not data:
                    return
                text = decoder.decode(data)
                if text:
                    events.append((t0 + time.monotonic() - start, text))
                    screen_since_mark += text

    speaking_until = 0.0     # monotonic time the current line ends
    for action, arg, after in SCRIPT:
        if action == "say":
            seconds = narrator.speak(arg)
            if seconds:
                pump(max(0.0, speaking_until + gap - time.monotonic()))
                narrator.place(t0 + time.monotonic() - start)
                speaking_until = time.monotonic() + seconds
        elif action == "wait_for":
            deadline = time.monotonic() + 30
            while arg not in screen_since_mark:
                if time.monotonic() > deadline:
                    tail = screen_since_mark[-1500:]
                    raise SystemExit(f"timed out waiting for {arg!r}; last output:\n{tail!r}")
                pump(0.05)
        elif action == "type":
            for ch in arg:
                os.write(fd, ch.encode())
                pump(random.uniform(0.05, 0.11))
        elif action == "key":
            os.write(fd, arg.encode())
        screen_since_mark = ""
        pump(after)

    pump(max(0.0, speaking_until + gap - time.monotonic()))
    os.write(fd, b"exit\r")
    pump(0.5)
    os.close(fd)
    os.waitpid(pid, 0)

    t_end = t0 + time.monotonic() - start
    events.append((t_end, END_CARD))
    end_seconds = narrator.speak(END_LINE)
    if end_seconds:
        narrator.place(t_end + 0.6)
    hold = max(4.5, 0.6 + end_seconds + 1.0)
    events.append((t_end + hold, " "))  # hold the end card
    narrator.write()

    with out.open("w") as f:
        f.write(json.dumps({"version": 2, "width": COLS, "height": ROWS,
                            "env": {"TERM": "xterm-256color", "SHELL": "/bin/zsh"}}) + "\n")
        for t, text in sorted(events, key=lambda e: e[0]):
            f.write(json.dumps([round(t, 4), "o", text]) + "\n")
    print(f"wrote {out} ({t_end + hold:.1f}s, {len(narrator.clips)} narration lines)")


if __name__ == "__main__":
    if sys.argv[1:] == ["--lines"]:
        print(json.dumps([{"text": t, "spoken": spoken(t)} for t in narration_lines()], indent=1))
        raise SystemExit
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]),
         Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else None)
