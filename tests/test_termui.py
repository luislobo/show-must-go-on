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


try:
    import curses
except ImportError:      # Windows without windows-curses
    curses = None


class KeysRanOut(BaseException):
    """The picker asked for a key the test did not give. A BaseException, so
    pick(), which turns any Exception into "return every item", cannot hide it."""


class FakeScreen:
    """Stands in for the curses window: plays the given keys (a str is a
    character, an int a curses key code) and keeps the text of each screen
    drawn, one dict of row -> text per key read."""

    def __init__(self, keys, h=20, w=100):
        self.keys = list(keys)
        self.h, self.w = h, w
        self.rows = {}
        self.frames = []

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

    def _next(self):
        self.frames.append(dict(self.rows))
        if not self.keys:
            raise KeysRanOut()
        return self.keys.pop(0)

    def getch(self):
        k = self._next()
        return ord(k) if isinstance(k, str) else k

    def get_wch(self):
        return self._next()


@unittest.skipIf(curses is None, "needs curses")
class PickerFilter(unittest.TestCase):
    ITEMS = ["dotfiles shell setup", "storefront checkout", "Dotfiles: git config",
             "infra modules", "Diseño del checkout"]

    def pick(self, keys, search=True, **kw):
        """Run the picker on the keys; every key must be used."""
        screen = FakeScreen(keys)
        with mock.patch.object(curses, "wrapper", lambda run: run(screen)), \
             mock.patch.object(curses, "curs_set", lambda n: None), \
             mock.patch.object(curses, "has_colors", lambda: False):
            got = termui.pick(self.ITEMS, title="t", row=lambda it, w: [(it, "")],
                              search=(lambda it: it) if search else None, **kw)
        self.assertEqual(screen.keys, [], "keys left over")
        return got, screen

    @staticmethod
    def rows(frame):
        return [frame[y].split("] ", 1)[1].strip() for y in sorted(frame) if "[ ]" in frame[y]
                or "[x]" in frame[y]]

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
        # q, j and k are letters here: they neither quit nor move
        got, screen = self.pick(["/", *"qjk", "\x1b", "q"])
        self.assertIsNone(got)
        typed = screen.frames[4]                   # drawn after "/", "q", "j", "k"
        self.assertIn("/ qjk", typed[1])
        self.assertIn("0 of 5", typed[1])

    def test_the_filter_line_shows_the_words_and_the_count(self):
        _, screen = self.pick(["/", *"dot", "\n", "q"])
        after_dot = screen.frames[4]
        self.assertIn("/ dot", after_dot[1])
        self.assertIn("2 of 5", after_dot[1])
        self.assertEqual(self.rows(after_dot), ["dotfiles shell setup", "Dotfiles: git config"])

    def test_letters_beyond_ascii_can_be_typed(self):
        got, screen = self.pick(["/", "ñ", "\n", "\n"])
        self.assertIn("/ ñ", screen.frames[2][1])
        self.assertEqual(got, ["Diseño del checkout"])

    def test_esc_clears_the_filter_before_it_quits(self):
        got, _ = self.pick(["/", *"dot", "\n", 27, "\n"])
        self.assertEqual(got, ["dotfiles shell setup"])   # the full list again, first item

    def test_backspace_widens_the_filter(self):
        got, _ = self.pick(["/", *"dotx", "\x7f", "\n", "a", "\n"])
        self.assertEqual(len(got), 2)

    def test_enter_with_no_match_picks_nothing(self):
        got, _ = self.pick(["/", *"zzz", "\n", "\n", "q"])
        self.assertIsNone(got)

    def test_ticks_outside_the_filter_are_kept_and_shown_as_hidden(self):
        got, screen = self.pick(["j", " ", "/", *"dot", "\n", "a", "\n"])
        self.assertIn("1 ticked, hidden", screen.frames[6][1])
        self.assertEqual(got, self.ITEMS[:3])

    def test_n_and_i_act_on_the_rows_shown(self):
        got, _ = self.pick(["a", "/", *"dot", "\n", "n", "\n"])
        self.assertEqual(got, ["storefront checkout", "infra modules", "Diseño del checkout"])
        got, _ = self.pick(["/", *"dot", "\n", "i", 27, "\n"])
        self.assertEqual(got, ["dotfiles shell setup", "Dotfiles: git config"])

    def test_space_ticks_the_highlighted_match_not_the_row_at_that_position(self):
        got, _ = self.pick(["/", *"infra", "\n", " ", 27, "\n"])
        self.assertEqual(got, ["infra modules"])

    def test_footer_and_details_follow_the_filter(self):
        seen = {"footer": [], "details": []}
        footer = lambda it: (seen["footer"].append(it), ([], ""))[1]
        details = lambda it, w: (seen["details"].append(it), [[(it, "")]])[1]
        _, screen = self.pick(["/", *"infra", "\n", curses.KEY_RIGHT, "h", "q"],
                              footer=footer, details=details)
        self.assertEqual(seen["footer"][-1], "infra modules")
        self.assertEqual(seen["details"], ["infra modules"])
        self.assertIn("1/1", screen.frames[-2][0])     # the details page counts the rows shown

    def test_end_goes_to_the_last_row_shown(self):
        got, _ = self.pick(["/", *"dot", "\n", "G", "\n"])
        self.assertEqual(got, ["Dotfiles: git config"])

    def test_arrows_move_while_typing(self):
        got, _ = self.pick(["/", *"dot", curses.KEY_DOWN, "\n", "\n"])
        self.assertEqual(got, ["Dotfiles: git config"])

    def test_the_highlighted_row_stays_when_the_filter_still_holds_it(self):
        got, _ = self.pick(["j", "j", "/", *"dot", "\n", "\n"])
        self.assertEqual(got, ["Dotfiles: git config"])

    def test_without_search_slash_does_nothing(self):
        got, screen = self.pick(["/", "\n"], search=False)
        self.assertEqual(got, ["dotfiles shell setup"])
        self.assertNotIn("filter", screen.rows[0])


if __name__ == "__main__":
    unittest.main()
