"""Tests for the pure freshness-classification logic (no HTTP involved)."""

from __future__ import annotations

import unittest

from app.freshness import Freshness, _parse_version, classify


class ParseVersionTests(unittest.TestCase):
    def test_parses_simple_version(self):
        self.assertEqual(_parse_version("1.2.3"), (1, 2, 3))

    def test_parses_two_segment_version(self):
        self.assertEqual(_parse_version("1.2"), (1, 2))

    def test_unparseable_version_returns_none(self):
        self.assertIsNone(_parse_version("not-a-version"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_version(""))

    def test_never_raises_on_garbage_input(self):
        for value in ("???", "1..2", "....", "v", "1.2.3.4.5.6"):
            try:
                _parse_version(value)
            except Exception as exc:  # pragma: no cover - failure path
                self.fail(f"_parse_version raised {exc!r} for {value!r}")


class ClassifyHappyPathTests(unittest.TestCase):
    def test_equal_versions_are_current(self):
        self.assertEqual(classify("1.2.3", "1.2.3"), Freshness.CURRENT)

    def test_older_current_is_outdated(self):
        self.assertEqual(classify("1.0.0", "1.2.0"), Freshness.OUTDATED)

    def test_current_ahead_of_latest_is_current(self):
        # A local build ahead of the snapshot is not an error condition.
        self.assertEqual(classify("2.0.0", "1.9.9"), Freshness.CURRENT)


class ClassifyBoundaryTests(unittest.TestCase):
    def test_differing_segment_counts_are_padded_before_compare(self):
        self.assertEqual(classify("1.2", "1.2.0"), Freshness.CURRENT)

    def test_differing_segment_counts_still_detect_outdated(self):
        self.assertEqual(classify("1.2", "1.3.0"), Freshness.OUTDATED)

    def test_prerelease_suffix_does_not_raise_and_is_deterministic(self):
        result_a = classify("1.2.3rc1", "1.2.3")
        result_b = classify("1.2.3rc1", "1.2.3")
        self.assertEqual(result_a, result_b)
        self.assertIn(result_a, (Freshness.CURRENT, Freshness.OUTDATED, Freshness.UNKNOWN))

    def test_build_metadata_suffix_does_not_raise_and_is_deterministic(self):
        result_a = classify("1.2.3+build5", "1.2.3")
        result_b = classify("1.2.3+build5", "1.2.3")
        self.assertEqual(result_a, result_b)


class ClassifyValidationTests(unittest.TestCase):
    def test_latest_none_is_unknown(self):
        self.assertEqual(classify("1.0.0", None), Freshness.UNKNOWN)

    def test_latest_empty_string_is_unknown(self):
        self.assertEqual(classify("1.0.0", ""), Freshness.UNKNOWN)

    def test_current_none_is_unknown(self):
        self.assertEqual(classify(None, "1.0.0"), Freshness.UNKNOWN)

    def test_both_none_is_unknown(self):
        self.assertEqual(classify(None, None), Freshness.UNKNOWN)

    def test_unparseable_current_is_unknown(self):
        self.assertEqual(classify("banana", "1.0.0"), Freshness.UNKNOWN)

    def test_unparseable_latest_is_unknown(self):
        self.assertEqual(classify("1.0.0", "banana"), Freshness.UNKNOWN)

    def test_classify_never_raises_on_garbage_input(self):
        cases = [(None, None), ("", ""), ("???", "???"), ("1..2", "1.2.3")]
        for current, latest in cases:
            try:
                classify(current, latest)
            except Exception as exc:  # pragma: no cover - failure path
                self.fail(f"classify raised {exc!r} for ({current!r}, {latest!r})")


if __name__ == "__main__":
    unittest.main()
