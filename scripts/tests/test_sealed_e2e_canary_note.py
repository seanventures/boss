"""Unit tests for scripts/qa/sealed_e2e_canary_note.py (no network, no spend).

BOSS-351 slice 9: "Prefer a one-line README note that this is a
sealed-e2e-canary. Hold for a human Decision." This module renders that
note and validates it is present in README.md as a single, honest line
that never claims a merge happened. The module does not exist yet; this
file defines the contract it must satisfy.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.qa.sealed_e2e_canary_note import (  # noqa: E402
    CANARY_MARKER,
    DEFAULT_PLANE_ID,
    extract_canary_note_lines,
    render_canary_note,
    validate_canary_note,
)
from scripts.qa.sealed_e2e_live import CANARY_TITLE, PLANE  # noqa: E402


class RenderCanaryNoteTests(unittest.TestCase):
    def test_happy_path_mentions_marker_and_plane_on_one_line(self):
        note = render_canary_note()
        self.assertIn(CANARY_MARKER, note)
        self.assertIn(DEFAULT_PLANE_ID, note)
        self.assertNotIn("\n", note)
        self.assertEqual(note, note.strip())

    def test_note_holds_for_decision_and_never_claims_a_merge(self):
        note = render_canary_note().lower()
        self.assertTrue("hold" in note or "decision" in note)
        self.assertNotIn("merged", note)
        self.assertNotIn("auto-merge", note)

    def test_custom_plane_id_replaces_default(self):
        note = render_canary_note("BOSS-999")
        self.assertIn("BOSS-999", note)
        self.assertNotIn(DEFAULT_PLANE_ID, note)

    def test_blank_plane_id_is_rejected(self):
        with self.assertRaises(ValueError):
            render_canary_note("   ")

    def test_marker_constant_matches_existing_canary_gate(self):
        # scripts/qa/sealed_e2e_live.py already defines the sealed-e2e-canary
        # contract this note must stay consistent with.
        self.assertEqual(CANARY_MARKER, CANARY_TITLE)
        self.assertEqual(DEFAULT_PLANE_ID, PLANE)


class ExtractCanaryNoteLinesTests(unittest.TestCase):
    def test_happy_path_finds_the_marker_line(self):
        text = "# BOSS\n\nSome intro.\n\n" + render_canary_note() + "\n\nMore docs.\n"
        lines = extract_canary_note_lines(text)
        self.assertEqual(lines, [render_canary_note()])

    def test_absent_marker_returns_empty_list(self):
        text = "# BOSS\n\nNothing canary-related here.\n"
        self.assertEqual(extract_canary_note_lines(text), [])

    def test_marker_inside_a_fenced_code_block_does_not_count(self):
        text = (
            "# BOSS\n\n"
            "```\n"
            f"echo {CANARY_MARKER}\n"
            "```\n"
        )
        self.assertEqual(extract_canary_note_lines(text), [])


class ValidateCanaryNoteTests(unittest.TestCase):
    def test_happy_path_accepts_rendered_note_in_a_readme(self):
        readme = "# BOSS\n\n" + render_canary_note() + "\n\nRest of the README.\n"
        verdict = validate_canary_note(readme)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["missing"], [])
        self.assertEqual(verdict["note"], render_canary_note())

    def test_missing_marker_is_the_most_likely_failure(self):
        verdict = validate_canary_note("# BOSS\n\nNo canary note in here.\n")
        self.assertFalse(verdict["ok"])
        self.assertIn("marker", verdict["missing"])
        self.assertIsNone(verdict["note"])

    def test_duplicated_note_lines_fail_the_one_line_requirement(self):
        note = render_canary_note()
        readme = f"# BOSS\n\n{note}\n\n{note}\n"
        verdict = validate_canary_note(readme)
        self.assertFalse(verdict["ok"])
        self.assertIn("single_line", verdict["missing"])

    def test_note_without_the_plane_id_fails_validation(self):
        readme = f"# BOSS\n\nThis is a {CANARY_MARKER} note with no plane id.\n"
        verdict = validate_canary_note(readme)
        self.assertFalse(verdict["ok"])
        self.assertIn("plane_id", verdict["missing"])

    def test_note_that_claims_a_merge_fails_validation(self):
        readme = (
            "# BOSS\n\n"
            f"This {CANARY_MARKER} PR for {DEFAULT_PLANE_ID} was already merged.\n"
        )
        verdict = validate_canary_note(readme)
        self.assertFalse(verdict["ok"])
        self.assertIn("no_merge_claim", verdict["missing"])

    def test_empty_readme_fails_closed_without_raising(self):
        verdict = validate_canary_note("")
        self.assertFalse(verdict["ok"])
        self.assertIn("marker", verdict["missing"])
        self.assertIsNone(verdict["note"])

    def test_accepts_a_note_rendered_for_a_non_default_plane_id(self):
        note = render_canary_note("BOSS-999")
        readme = "# BOSS\n\n" + note + "\n\nRest of the README.\n"
        verdict = validate_canary_note(readme, plane_id="BOSS-999")
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["missing"], [])
        self.assertEqual(verdict["note"], note)

    def test_note_for_a_different_plane_id_fails_validation(self):
        note = render_canary_note("BOSS-999")
        readme = "# BOSS\n\n" + note + "\n"
        verdict = validate_canary_note(readme, plane_id=DEFAULT_PLANE_ID)
        self.assertFalse(verdict["ok"])
        self.assertIn("plane_id", verdict["missing"])

    def test_merge_claim_detection_is_case_insensitive(self):
        readme = (
            "# BOSS\n\n"
            f"This {CANARY_MARKER} PR for {DEFAULT_PLANE_ID} was already MERGED.\n"
        )
        verdict = validate_canary_note(readme)
        self.assertFalse(verdict["ok"])
        self.assertIn("no_merge_claim", verdict["missing"])

    def test_duplicated_notes_report_every_missing_reason_together(self):
        readme = (
            "# BOSS\n\n"
            f"This is a {CANARY_MARKER} note with no plane id.\n\n"
            f"This is a {CANARY_MARKER} note with no plane id.\n"
        )
        verdict = validate_canary_note(readme)
        self.assertFalse(verdict["ok"])
        self.assertIn("single_line", verdict["missing"])
        self.assertIn("plane_id", verdict["missing"])
        self.assertIsNone(verdict["note"])


if __name__ == "__main__":
    unittest.main()
