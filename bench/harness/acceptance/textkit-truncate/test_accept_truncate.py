"""Held by the benchmark, never shown to the agent: run on the pull request's head."""
import unittest

import textkit
from textkit import truncate_words


class AcceptTruncate(unittest.TestCase):
    def test_it_is_exported(self):
        self.assertIn("truncate_words", textkit.__all__)

    def test_cuts_to_the_limit_and_adds_the_suffix(self):
        self.assertEqual(truncate_words("the quick brown fox jumps", 3), "the quick brown…")
        self.assertEqual(truncate_words("a  b   c", 2), "a b…")
        self.assertEqual(truncate_words("a b c", 0), "…")

    def test_a_text_within_the_limit_comes_back_unchanged(self):
        self.assertEqual(truncate_words("a  b", 2), "a  b")
        self.assertEqual(truncate_words("", 3), "")

    def test_the_suffix_is_a_parameter(self):
        self.assertEqual(truncate_words("a b c", 2, suffix=" [more]"), "a b [more]")
        self.assertEqual(truncate_words("a b c", 2, ""), "a b")

    def test_a_negative_limit_raises(self):
        with self.assertRaises(ValueError):
            truncate_words("a b", -1)


if __name__ == "__main__":
    unittest.main()
