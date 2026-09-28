"""Tests for the checked-in dependency snapshot loader (no HTTP involved)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.snapshot import DependencyRecord, load_snapshot


class LoadSnapshotHappyPathTests(unittest.TestCase):
    def test_loads_well_formed_snapshot(self):
        data = [
            {"name": "fastapi", "current_version": "0.100.0", "latest_version": "0.115.0"},
            {"name": "uvicorn", "current_version": "0.30.0", "latest_version": "0.30.0"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dependencies.json"
            path.write_text(json.dumps(data))
            records = load_snapshot(path)

        self.assertEqual(len(records), 2)
        self.assertTrue(all(isinstance(record, DependencyRecord) for record in records))
        self.assertEqual(records[0].name, "fastapi")
        self.assertEqual(records[0].current_version, "0.100.0")
        self.assertEqual(records[0].latest_version, "0.115.0")


class LoadSnapshotEdgeCaseTests(unittest.TestCase):
    def test_empty_list_returns_empty_list_not_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dependencies.json"
            path.write_text("[]")
            records = load_snapshot(path)
        self.assertEqual(records, [])

    def test_null_latest_version_is_preserved_as_none(self):
        data = [{"name": "foo", "current_version": "1.0.0", "latest_version": None}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dependencies.json"
            path.write_text(json.dumps(data))
            records = load_snapshot(path)
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0].latest_version)


class LoadSnapshotValidationTests(unittest.TestCase):
    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.json"
            with self.assertRaises(Exception):
                load_snapshot(missing)

    def test_malformed_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dependencies.json"
            path.write_text("{not valid json")
            with self.assertRaises(Exception):
                load_snapshot(path)

    def test_record_missing_required_field_raises(self):
        data = [{"name": "foo"}]  # no current_version
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dependencies.json"
            path.write_text(json.dumps(data))
            with self.assertRaises(Exception):
                load_snapshot(path)


if __name__ == "__main__":
    unittest.main()
