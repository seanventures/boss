import unittest

from recipes import count_recipes


class CountRecipesTest(unittest.TestCase):
    def test_counts_named_recipes(self):
        self.assertEqual(count_recipes(["Soup", "Bread"]), 2)

    def test_skips_blank_names(self):
        self.assertEqual(count_recipes(["Soup", " ", None]), 1)


if __name__ == "__main__":
    unittest.main()
