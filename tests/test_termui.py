"""Tests for termui.py: colour switches, ANSI paint, wrapping, the tagline and
the picker's filter.

Stdlib only. Run from the repo root:  python3 -m unittest discover tests
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import termui  # noqa: E402


class ColorsEnabled(unittest.TestCase):
    def test_no_color(self):
        self.assertTrue(termui.colors_enabled({}, True))
        self.assertFalse(termui.colors_enabled({"NO_COLOR": "1"}, True))
        self.assertTrue(termui.colors_enabled({"NO_COLOR": ""}, True))
        self.assertFalse(termui.colors_enabled({}, False))


class ProgressColors(unittest.TestCase):
    def test_paint_on_a_terminal(self):
        with mock.patch.object(termui, "stdout_colors", return_value=True):
            self.assertEqual(termui.paint(3, "ok"), "\033[32m3\033[0m")
            self.assertEqual(termui.paint("x", "bold"), "\033[1mx\033[0m")
            self.assertEqual(termui.paint("x", ""), "x")

    def test_plain_when_colour_is_off(self):
        with mock.patch.object(termui, "stdout_colors", return_value=False):
            self.assertEqual(termui.paint("x", "warn"), "x")

    def test_off_when_piped_no_color_or_dumb(self):
        with mock.patch.object(sys.stdout, "isatty", return_value=False):
            self.assertFalse(termui.stdout_colors())
        with mock.patch.object(sys.stdout, "isatty", return_value=True):
            with mock.patch.dict(os.environ, {"NO_COLOR": "1", "TERM": "xterm"}):
                self.assertFalse(termui.stdout_colors())
            with mock.patch.dict(os.environ, {"TERM": "dumb"}):
                os.environ.pop("NO_COLOR", None)
                self.assertFalse(termui.stdout_colors())
            with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
                os.environ.pop("NO_COLOR", None)
                self.assertTrue(termui.stdout_colors())

    def test_only_normal_colours(self):
        self.assertEqual(set(termui.ANSI.values()), {"31", "32", "33", "36", "1"})

    def test_every_role_paints(self):
        with mock.patch.object(termui, "stdout_colors", return_value=True):
            for role in list(termui.ROLE_COLORS) + ["bold"]:
                self.assertTrue(termui.paint("x", role).startswith("\033["), role)
            line = "".join(termui.paint(t, r) for t, r in termui.TAGLINE)
            self.assertIn("\033[31m♥", line)


class TextFitting(unittest.TestCase):
    def test_clip(self):
        self.assertEqual(termui.clip("a  b\nc", 10), "a b c")
        self.assertEqual(termui.clip("abcdefghij", 5), "abcd…")
        self.assertEqual(termui.clip(None, 5), "")

    def test_wrap_caps_lines_with_an_ellipsis(self):
        lines = termui.wrap("word " * 100, 20, max_lines=3)
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(len(line) <= 20 for line in lines))
        self.assertTrue(lines[-1].endswith("…"))
        self.assertEqual(termui.wrap("", 20), [""])


class Tagline(unittest.TestCase):
    def test_red_is_only_on_symbols(self):
        self.assertEqual("".join(t for t, _ in termui.TAGLINE).strip(), "Proudly made in Dallas with ♥")
        self.assertEqual([t for t, r in termui.TAGLINE if r == "heart"], ["♥"])
        red = {r for r, c in termui.ROLE_COLORS.items() if c == "COLOR_RED"}
        self.assertEqual(red, {"heart", "alert"})


class ImportBesideScript(unittest.TestCase):
    def test_restore_finds_termui_in_isolated_mode(self):
        """-I leaves the script's folder off sys.path; restore must add it itself."""
        import subprocess
        script = Path(__file__).resolve().parent.parent / "restore_terminals.py"
        r = subprocess.run([sys.executable, "-I", str(script), "--help"],
                           capture_output=True, text=True, cwd="/")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usage: restore", r.stdout)


if __name__ == "__main__":
    unittest.main()


try:
    import curses
except ImportError:      # Windows without windows-curses
    curses = None


class FakeScreen:
    """Stands in for the curses window: plays the given keys and keeps the
    text of the last screen drawn, one string per row."""

    def __init__(self, keys, h=20, w=100):
        self.keys = [ord(k) if isinstance(k, str) else k for k in keys]
        self.h, self.w = h, w
        self.rows = {}

    def keypad(self, on):
        pass

    def erase(self):
        self.rows = {}

    def getmaxyx(self):
        return self.h, self.w

    def addnstr(self, y, x, text, n, attr):
        line = self.rows.get(y, "").ljust(x)
        self.rows[y] = line[:x] + text[:n] + line[x + len(text[:n]):]

    def refresh(self):
        pass

    def getch(self):
        if not self.keys:
            raise AssertionError("the picker wanted more keys")
        return self.keys.pop(0)


@unittest.skipIf(curses is None, "needs curses")
class PickerFilter(unittest.TestCase):
    ITEMS = ["dotfiles shell setup", "storefront checkout", "Dotfiles: git config", "infra modules"]

    def pick(self, keys, search=True):
        screen = FakeScreen(keys)
        with mock.patch.object(curses, "wrapper", lambda run: run(screen)), \
             mock.patch.object(curses, "curs_set", lambda n: None), \
             mock.patch.object(curses, "has_colors", lambda: False):
            got = termui.pick(self.ITEMS, title="t", row=lambda it, w: [(it, "")],
                              search=(lambda it: it) if search else None)
        return got, screen

    def test_words_match_in_any_order_ignoring_case(self):
        self.assertTrue(termui.matches("Dotfiles: git config", "GIT dot"))
        self.assertFalse(termui.matches("Dotfiles: git config", "dot shell"))
        self.assertTrue(termui.matches("anything", "  "))

    def test_filter_then_tick_all_shown_and_confirm(self):
        got, _ = self.pick(["/", *"dot", "\n", "a", "\n"])
        self.assertEqual(got, ["dotfiles shell setup", "Dotfiles: git config"])

    def test_f_opens_the_filter_too_and_enter_alone_takes_the_highlighted_match(self):
        got, _ = self.pick(["f", *"infra", "\n", "\n"])
        self.assertEqual(got, ["infra modules"])

    def test_keys_go_to_the_filter_while_typing(self):
        # q, j and k are letters here, not quit and movement
        got, screen = self.pick(["/", *"qjk", 27, "q"])
        self.assertIsNone(got)

    def test_the_filter_line_shows_the_words_and_the_count(self):
        screen = FakeScreen(["/", *"dot", "\n", "q"])
        lines = []
        real_getch = screen.getch
        screen.getch = lambda: (lines.append(dict(screen.rows)), real_getch())[1]
        with mock.patch.object(curses, "wrapper", lambda run: run(screen)), \
             mock.patch.object(curses, "curs_set", lambda n: None), \
             mock.patch.object(curses, "has_colors", lambda: False):
            termui.pick(self.ITEMS, title="t", row=lambda it, w: [(it, "")], search=lambda it: it)
        after_dot = lines[4]          # drawn after "/", "d", "o", "t"
        self.assertIn("/ dot", after_dot[1])
        self.assertIn("2 of 4", after_dot[1])
        shown = [after_dot[y] for y in sorted(after_dot) if y >= 2 and "[ ]" in after_dot[y]]
        self.assertEqual(len(shown), 2)

    def test_esc_clears_the_filter_before_it_quits(self):
        got, _ = self.pick(["/", *"dot", "\n", 27, "\n"])
        self.assertEqual(got, ["dotfiles shell setup"])   # the full list again, first item

    def test_backspace_widens_the_filter(self):
        got, _ = self.pick(["/", *"dotx", 127, "\n", "a", "\n"])
        self.assertEqual(len(got), 2)

    def test_enter_with_no_match_picks_nothing(self):
        got, _ = self.pick(["/", *"zzz", "\n", "\n", "q"])
        self.assertIsNone(got)

    def test_ticks_outside_the_filter_are_kept(self):
        got, _ = self.pick(["j", " ", "/", *"dot", "\n", "a", "\n"])
        self.assertEqual(got, self.ITEMS[:3])

    def test_without_search_slash_does_nothing(self):
        got, screen = self.pick(["/", "\n"], search=False)
        self.assertEqual(got, ["dotfiles shell setup"])
        self.assertNotIn("filter", screen.rows[0])
