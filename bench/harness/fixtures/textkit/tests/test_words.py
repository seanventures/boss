import unittest

from textkit import first_words, word_count


class WordCountTest(unittest.TestCase):
    def test_counts_words_separated_by_one_space(self):
        self.assertEqual(word_count("the quick brown fox"), 4)

    def test_nothing_has_no_words(self):
        self.assertEqual(word_count(""), 0)


class FirstWordsTest(unittest.TestCase):
    def test_takes_the_first_words(self):
        self.assertEqual(first_words("one two three four", 2), "one two")

    def test_more_than_there_are_is_all_of_them(self):
        self.assertEqual(first_words("one two", 5), "one two")

    def test_a_negative_count_is_refused(self):
        with self.assertRaises(ValueError):
            first_words("one", -1)


if __name__ == "__main__":
    unittest.main()
