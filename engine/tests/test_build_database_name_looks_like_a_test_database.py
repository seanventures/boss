"""A build's own throwaway database must be named like a test database, so the suite guards
(apps/api/tests/db_guard.py, engine/tests/identity_s5_support.py) stop refusing to run against
it. The guards themselves are not touched here: only the build's own database prefix is.
"""
from __future__ import annotations

import contextlib
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/api/tests"))

from boss_engine.services import build_services as bs  # noqa: E402

import db_guard  # noqa: E402  (apps/api/tests/db_guard.py)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """Stands in for a psycopg connection: answers the catalog scan and records DROPs, without
    a real server. Good enough to prove the reaper's SQL still matches an old-prefix name."""

    def __init__(self, rows):
        self.rows = rows
        self.dropped = []

    def execute(self, query, params=None):
        text_ = str(query)
        if "pg_database" in text_ and "shobj_description" in text_:
            prefixes = [p for p in (params or ()) if isinstance(p, str)]
            matched = [(n, c) for n, c in self.rows if any(n.startswith(p) for p in prefixes)]
            return FakeResult(matched)
        if "DROP DATABASE" in text_ or "DROP ROLE" in text_:
            self.dropped.append(text_)
            return FakeResult([])
        if "SELECT EXISTS" in text_:
            return FakeResult([(False, False)])
        return FakeResult([])

    def close(self):
        pass


class BuildDatabaseNamedLikeATestDatabaseTests(unittest.TestCase):
    def test_p0_default_prefix_is_test_named_and_server_valid(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop(bs.PREFIX_ENV, None)
            pre = bs.prefix()
        self.assertIn("test", pre)
        name = pre + ("a" * 16)
        self.assertTrue(bs._NAME_OK.match(name), f"{name!r} must satisfy build_services._NAME_OK")
        self.assertLessEqual(len(name), 41)

    def test_p0_provisioned_name_passes_both_test_database_guards(self):
        # Actually provision (against a fake server), so the NAME under test is the one
        # provision() mints — not a hand-built string that could drift from the real code.
        fake_server = mock.Mock(host="build-services", port=5433)
        conn = FakeConn([])
        with mock.patch.object(bs, "server", return_value=fake_server), \
             mock.patch.object(bs, "_connect", return_value=conn), \
             mock.patch.object(bs, "_LEASES", {}), \
             mock.patch.object(bs, "_PENDING_CLEANUP", {}):
            lease = bs.provision("crun_name_check")
        name = lease.name
        self.assertIn("test", name, f"provisioned name {name!r} must read as a test database")
        url = f"postgresql://role:pw@build-services:5433/{name}"

        safe = db_guard.assert_safe_to_wipe(url)
        self.assertEqual(safe, url)

        import sys as _sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import identity_s5_support as support  # noqa: E402

        with mock.patch.dict("os.environ", {"DATABASE_URL": url}, clear=False):
            try:
                support.database_url()
            except RuntimeError as exc:
                self.fail(f"a build's own database was refused as not test-named: {exc}")

    def test_p1_reaper_still_recognizes_a_legacy_boss_run_prefixed_database(self):
        legacy_name = "boss_run_" + "f" * 16
        current_name = bs.prefix() + "e" * 16
        old_comment = json.dumps({"boss": "build-services", "run": bs.run_tag("crun_old_gone"),
                                  "install": "", "created": int(time.time()) - 10_000})
        conn = FakeConn([(legacy_name, old_comment), (current_name, old_comment)])

        fake_server = mock.Mock(host="fake-build-services", port=5433)
        with mock.patch.object(bs, "server", return_value=fake_server), \
             mock.patch.object(bs, "_connect", return_value=conn), \
             mock.patch.object(bs, "_LEASES", {}), \
             mock.patch.object(bs, "_PENDING_CLEANUP", {}):
            # Default min_age == REAP_MIN_AGE_SECONDS (300s); both comments are 10_000s old,
            # so this proves the reaper drops a legacy boss_run_ database PAST the threshold.
            report = bs.reap(set())

        self.assertIn(legacy_name, report["dropped"],
                      "the reaper must still reap pre-existing boss_run_<hex> databases")
        self.assertIn(current_name, report["dropped"])


if __name__ == "__main__":
    unittest.main()
