import unittest

from textkit import word_count


class WordCountSpacingTest(unittest.TestCase):
    def test_runs_of_spaces_are_one_gap(self):
        self.assertEqual(word_count("the  quick   fox"), 3)

    def test_tabs_and_newlines_separate_words(self):
        self.assertEqual(word_count("one\ttwo\nthree"), 3)

    def test_only_spaces_is_no_words(self):
        self.assertEqual(word_count("   "), 0)


if __name__ == "__main__":
    unittest.main()
