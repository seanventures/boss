import unittest

from tempconv import c_to_f, convert, f_to_c


class TempconvTest(unittest.TestCase):
    def test_boiling_and_freezing(self):
        self.assertEqual(c_to_f(100), 212)
        self.assertEqual(f_to_c(32), 0)

    def test_a_reading_is_converted_with_one_decimal(self):
        self.assertEqual(convert("100C"), "212.0F")
        self.assertEqual(convert("32F"), "0.0C")

    def test_nonsense_is_refused(self):
        with self.assertRaises(ValueError):
            convert("hot")


if __name__ == "__main__":
    unittest.main()
