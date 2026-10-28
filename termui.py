"""termui — terminal UI helpers: colour roles, ANSI paint for progress lines,
word wrap, and a curses multi-select picker with an optional details page.

Standard library only, Python 3.9+, no other file needed: copy it into
another project to reuse it.

Text is drawn in (text, role) pieces. A role names a colour from the
terminal's own palette, so the output follows its light or dark theme:

    accent  cyan    labels, keys, the current-row marker
    ok      green   ticked items, results, good states
    warn    yellow  interrupted or failed items, search hits
    heart   red     the tagline's ♥ only
    bold    bold in the default colour (no colour)
    ""      plain

Only the normal (0-7) colours are used: their contrast holds on light and
dark themes, where the bright ones fall below 4.5:1 on white. Colour is
never combined with bold, which some terminals render as the bright
variant. Red is kept for the ♥: a symbol needs 3:1, which red clears on
light, dark and Solarized themes, and text in red does not. NO_COLOR
(https://no-color.org) turns colour off.
"""

from __future__ import annotations

import os
import sys
import textwrap
from typing import Callable, Optional, Sequence, Tuple, TypeVar

T = TypeVar("T")

ROLE_COLORS = {"accent": "COLOR_CYAN", "ok": "COLOR_GREEN", "warn": "COLOR_YELLOW",
               "heart": "COLOR_RED"}
TAGLINE = [("Proudly made in Dallas with ", ""), ("♥", "heart"), (" ", "")]

# The same roles as ANSI codes for progress lines, plus bold (default
# colour) for numbers.
ANSI = {"accent": "36", "ok": "32", "warn": "33", "heart": "31", "bold": "1"}


def colors_enabled(env, has_colors: bool) -> bool:
    """https://no-color.org: any non-empty NO_COLOR turns colour off."""
    return has_colors and not env.get("NO_COLOR")


def stdout_colors() -> bool:
    """Colour progress output only on a terminal, and not under NO_COLOR or TERM=dumb."""
    return sys.stdout.isatty() and colors_enabled(os.environ, os.environ.get("TERM") != "dumb")


def paint(text, role: str) -> str:
    """Wrap text in the ANSI code for a role; plain when colour is off."""
    text = str(text)
    if not role or not stdout_colors():
        return text
    return f"\033[{ANSI[role]}m{text}\033[0m"


def clip(text: Optional[str], n: int) -> str:
    """Collapse whitespace and cut to n characters, ending on an ellipsis."""
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def wrap(text: Optional[str], width: int, max_lines: int = 0) -> list[str]:
    """Word-wrap text with collapsed whitespace; past max_lines, end on an ellipsis."""
    lines = textwrap.wrap(" ".join((text or "").split()), max(10, width)) or [""]
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(0, width - 1)] + "…"
    return lines


def pick(
    items: Sequence[T],
    *,
    title: str,
    noun: str = "item",
    row: Callable[[T, int], list[tuple[str, str]]],
    footer: Optional[Callable[[T], Tuple[list[tuple[str, str]], str]]] = None,
    details: Optional[Callable[[T, int], list[list[tuple[str, str]]]]] = None,
    tagline: Optional[list[tuple[str, str]]] = None,
) -> Optional[list[T]]:
    """Multi-select picker. Returns the ticked items (or the highlighted one when
    none is ticked), or None when the user quits. Requires a real TTY.

    noun                  what an item is called on the details page ("session 2/11")
    row(item, width)      the pieces of one list row, after the gutter and checkbox
    footer(item)          (location pieces, description) for the highlighted item
    details(item, width)  the lines of its details page; → opens it when given
    tagline               pieces right-aligned on the footer line when they fit
    """
    if not items:
        return []
    try:
        import curses
    except ImportError:
        print("curses not available — skipping TUI, returning all items.", file=sys.stderr)
        return list(items)

    selected = [False] * len(items)

    def run(stdscr):
        curses.curs_set(0)
        stdscr.keypad(True)
        role_attr = {role: curses.A_NORMAL for role in ROLE_COLORS}
        if curses.has_colors():
            # curses.wrapper has already called start_color(); without
            # use_default_colors() every cell is drawn white on black, which
            # overrides a light theme. -1 keeps the terminal's own colours.
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            if colors_enabled(os.environ, True):
                for n, (role, color) in enumerate(ROLE_COLORS.items(), 1):
                    curses.init_pair(n, getattr(curses, color), bg)
                    role_attr[role] = curses.color_pair(n)

        def put(y: int, segs: list[tuple[str, str]], bold: bool = False) -> None:
            """Draw (text, role) pieces left to right, clipped to the screen width."""
            x = 0
            limit = w - 1  # the bottom-right cell cannot be written
            for text, role in segs:
                if x >= limit:
                    break
                if role == "bold":
                    attr = curses.A_BOLD
                elif role:
                    attr = role_attr[role]
                else:
                    attr = curses.A_BOLD if bold else curses.A_NORMAL
                try:
                    stdscr.addnstr(y, x, text, limit - x, attr)
                except curses.error:
                    pass
                x += len(text)

        def with_tagline(segs: list[tuple[str, str]]) -> list[tuple[str, str]]:
            """The line's pieces, with the tagline right-aligned when there is room."""
            if not tagline:
                return segs
            used = sum(len(t) for t, _ in segs)
            tag_w = sum(len(t) for t, _ in tagline)
            if used + tag_w + 2 < w:
                return segs + [(" " * (w - 1 - used - tag_w), "")] + tagline
            return segs

        def keys(*pairs: tuple[str, str]) -> list[tuple[str, str]]:
            segs: list[tuple[str, str]] = []
            for key, what in pairs:
                segs += [("   ", ""), (f"[{key}]", "accent"), (f" {what}", "")]
            return segs

        idx = 0
        top = 0
        detail = False   # showing the details page for items[idx]
        dtop = 0         # first visible line of the details page
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            if detail:
                lines = details(items[idx], w - 1)
                body_h = max(1, h - 2)
                dtop = max(0, min(dtop, len(lines) - body_h))
                mark = [("[x] selected", "ok")] if selected[idx] else [("[ ] not selected", "")]
                put(0, [(f" {noun} {idx + 1}/{len(items)} — ", "")] + mark +
                    keys(("←", "back"), ("space", "toggle"), ("j/k", "scroll")), bold=True)
                for line_no, line in enumerate(lines[dtop:dtop + body_h], 1):
                    put(line_no, line)
                last = min(len(lines), dtop + body_h)
                more = "  ↓ more" if last < len(lines) else ""
                put(h - 1, with_tagline([(f" lines {dtop + 1}–{last} of {len(lines)}", ""),
                                         (more, "accent")]))
                stdscr.refresh()
                c = stdscr.getch()
                if c in (curses.KEY_LEFT, ord("h"), ord("q"), 27):  # back to the list
                    detail = False
                elif c in (curses.KEY_DOWN, ord("j")):
                    dtop += 1
                elif c in (curses.KEY_UP, ord("k")):
                    dtop = max(0, dtop - 1)
                elif c in (curses.KEY_NPAGE, ord("f")):
                    dtop += body_h
                elif c in (curses.KEY_PPAGE, ord("b")):
                    dtop = max(0, dtop - body_h)
                elif c in (curses.KEY_HOME, ord("g")):
                    dtop = 0
                elif c in (curses.KEY_END, ord("G")):
                    dtop = len(lines)
                elif c == ord(" "):
                    selected[idx] = not selected[idx]
                continue
            # header (1) + 2-line footer (2) + 1 buffer = 4 lines reserved
            body_h = max(1, h - 4)
            # scroll
            if idx < top:
                top = idx
            elif idx >= top + body_h:
                top = idx - body_h + 1
            # header: title, selection count, then each key in the accent colour
            count_sel = sum(selected)
            header = [(f" {title} — ", ""),
                      (f"{count_sel}/{len(items)} selected", "ok" if count_sel else "")]
            header += keys(("space", "toggle"), *([("→", "details")] if details else []),
                           ("a", "all"), ("n", "none"), ("i", "invert"),
                           ("enter", "confirm"), ("q", "quit"))
            put(0, header, bold=True)
            # rows: a marker in the gutter and bold text for the current row
            for i in range(top, min(len(items), top + body_h)):
                cur_row = i == idx
                segs = [("▶ " if cur_row else "  ", "accent"),
                        ("[x]" if selected[i] else "[ ]", "ok" if selected[i] else ""),
                        (" ", "")]
                put(1 + (i - top), segs + row(items[i], w - 7), bold=cur_row)
            # footer: line 1 = location pieces, line 2 = description
            loc_segs, desc_line = footer(items[idx]) if footer else ([], "")
            put(h - 2, with_tagline(loc_segs))
            put(h - 1, [(clip(desc_line, w - 1), "")])
            stdscr.refresh()
            c = stdscr.getch()
            if c in (ord("q"), 27):  # q or ESC
                return None
            if details and c in (curses.KEY_RIGHT, ord("l")):
                detail, dtop = True, 0
            elif c in (curses.KEY_DOWN, ord("j")):
                idx = min(len(items) - 1, idx + 1)
            elif c in (curses.KEY_UP, ord("k")):
                idx = max(0, idx - 1)
            elif c == curses.KEY_NPAGE:
                idx = min(len(items) - 1, idx + body_h)
            elif c == curses.KEY_PPAGE:
                idx = max(0, idx - body_h)
            elif c in (curses.KEY_HOME, ord("g")):
                idx = 0
            elif c in (curses.KEY_END, ord("G")):
                idx = len(items) - 1
            elif c == ord(" "):
                selected[idx] = not selected[idx]
            elif c == ord("a"):
                for i in range(len(selected)):
                    selected[i] = True
            elif c == ord("n"):
                for i in range(len(selected)):
                    selected[i] = False
            elif c == ord("i"):
                for i in range(len(selected)):
                    selected[i] = not selected[i]
            elif c in (10, 13, curses.KEY_ENTER):
                picked = [it for it, keep in zip(items, selected) if keep]
                if not picked:
                    picked = [items[idx]]
                return picked

    try:
        return curses.wrapper(run)
    except Exception as e:
        print(f"TUI error: {e}", file=sys.stderr)
        return list(items)
