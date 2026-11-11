"""Held by the benchmark, never shown to the agent: run on the pull request's head."""
import unittest

from textkit import first_words, word_count


class AcceptWordCount(unittest.TestCase):
    def test_whitespace_of_any_kind_and_length_separates_words(self):
        cases = {
            "a  b": 2,
            " a b ": 2,
            "a\nb\tc": 3,
            "   ": 0,
            "": 0,
            "\n\n": 0,
            "one": 1,
            "the quick brown fox": 4,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(word_count(text), expected)

    def test_first_words_still_works(self):
        self.assertEqual(first_words("a  b   c", 2), "a b")


if __name__ == "__main__":
    unittest.main()
