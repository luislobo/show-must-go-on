"""Record the demo as an asciicast v2 file by driving a shell in a pty.

    python3 demo/record.py <demo-home> <out.cast>

Runs zsh with HOME=<demo-home> and demo/bin first on PATH (stand-ins for
`claude -p` and `gh`), types the steps in SCRIPT on a fixed timeline, and
waits for the screen to show what each step expects before moving on.
A title card and an end card are written straight into the cast.
"""

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

# (action, argument, pause after in seconds)
SCRIPT = [
    ("wait_for", "$ ", 0.8),
    ("type", "restore", 0.3),
    ("key", ENTER, 0.0),
    ("wait_for", "selected", 2.2),     # the picker is up
    ("key", "j", 0.7),
    ("key", " ", 0.8),                 # tick the second session
    ("key", "j", 0.6),
    ("key", "j", 0.6),
    ("key", " ", 0.9),                 # tick the fourth
    ("key", "k", 0.5),
    ("key", "k", 0.5),
    ("key", "k", 0.7),
    ("key", RIGHT, 3.2),               # details of the first session
    ("key", "j", 0.35), ("key", "j", 0.35), ("key", "j", 0.35),
    ("key", "j", 0.35), ("key", "j", 0.35), ("key", "j", 2.0),
    ("key", LEFT, 1.2),
    ("key", ENTER, 0.0),
    ("wait_for", "dry-run", 3.5),
    ("key", "\x0c", 0.8),              # Ctrl-L: zsh clears the screen
    ("type", "restore --find backoff", 0.3),
    ("key", ENTER, 0.0),
    ("wait_for", "selected", 2.2),
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


def main(home: Path, out: Path) -> None:
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

    def emit(text: str, hold: float) -> None:
        nonlocal t0
        events.append((t0, text))
        t0 += hold

    emit(TITLE_CARD, 3.8)
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

    for action, arg, after in SCRIPT:
        if action == "wait_for":
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

    os.write(fd, b"exit\r")
    pump(0.5)
    os.close(fd)
    os.waitpid(pid, 0)

    t_end = t0 + time.monotonic() - start
    events.append((t_end, END_CARD))
    events.append((t_end + 4.5, " "))  # hold the end card

    with out.open("w") as f:
        f.write(json.dumps({"version": 2, "width": COLS, "height": ROWS,
                            "env": {"TERM": "xterm-256color", "SHELL": "/bin/zsh"}}) + "\n")
        for t, text in sorted(events, key=lambda e: e[0]):
            f.write(json.dumps([round(t, 4), "o", text]) + "\n")
    print(f"wrote {out} ({t_end + 4.5:.1f}s)")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]))
