"""Tests for termui.py: colour switches, ANSI paint, wrapping and the tagline.

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
    def test_heart_is_the_only_red(self):
        self.assertEqual("".join(t for t, _ in termui.TAGLINE).strip(), "Proudly made in Dallas with ♥")
        self.assertEqual([t for t, r in termui.TAGLINE if r == "heart"], ["♥"])
        self.assertEqual(termui.ROLE_COLORS["heart"], "COLOR_RED")


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
