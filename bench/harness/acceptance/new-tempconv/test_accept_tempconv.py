"""Held by the benchmark, never shown to the agent: run on the pull request's head."""
import subprocess
import sys
import unittest

from tempconv import c_to_f, convert, f_to_c


class AcceptTempconv(unittest.TestCase):
    def test_the_functions(self):
        self.assertAlmostEqual(c_to_f(37), 98.6, places=6)
        self.assertAlmostEqual(f_to_c(-40), -40, places=6)
        self.assertAlmostEqual(f_to_c(c_to_f(21.5)), 21.5, places=6)

    def test_readings(self):
        self.assertEqual(convert("100C"), "212.0F")
        self.assertEqual(convert("-40F"), "-40.0C")
        self.assertEqual(convert("36.6c"), "97.9F")
        for bad in ("", "C", "12K", "abc", "1 2C"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                convert(bad)

    def _cli(self, *args):
        return subprocess.run([sys.executable, "-m", "tempconv", *args],
                              capture_output=True, text=True, timeout=30)

    def test_the_command_line(self):
        ok = self._cli("100C")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(ok.stdout.strip(), "212.0F")
        bad = self._cli("warm")
        self.assertEqual(bad.returncode, 2)
        self.assertTrue(bad.stderr.strip())
        self.assertEqual(self._cli().returncode, 2)


if __name__ == "__main__":
    unittest.main()
