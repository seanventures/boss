import unittest

from textkit import truncate_words


class TruncateWordsTest(unittest.TestCase):
    def test_a_long_text_is_cut_with_an_ellipsis(self):
        self.assertEqual(truncate_words("one two three four", 2), "one two…")

    def test_a_short_text_is_unchanged(self):
        self.assertEqual(truncate_words("one two", 5), "one two")

    def test_the_suffix_can_be_chosen(self):
        self.assertEqual(truncate_words("a b c", 1, suffix="..."), "a...")

    def test_a_negative_limit_is_refused(self):
        with self.assertRaises(ValueError):
            truncate_words("a", -1)


if __name__ == "__main__":
    unittest.main()
