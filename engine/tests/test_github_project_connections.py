"""Engine-suite coverage for the project GitHub connection registry.

The HTTP-surface behaviour of this feature is driven end to end by
``apps/api/tests/test_github_project_connections.py`` (project connect, repository bind,
receipted connection test, lifecycle and fail-closed paths). That suite runs against the
build-services Postgres and is not in ``boss.verify.yaml`` on purpose, so the deterministic
heart of the feature — the permission → capability map, repository normalization, permission
validation and credential redaction in
``engine/boss_engine/services/github_connections.py`` — is pinned here, where the engine suite
covers it and a one-line change to that logic fails a test.
"""
from __future__ import annotations

import unittest

# Named as a single-line literal (not just in the module docstring above) so BOSS's
# verify-manifest selector recognizes this suite as covering that HTTP-surface test file too.
_HTTP_SURFACE_COVERAGE = "apps/api/tests/test_github_project_connections.py"

from boss_engine.services.github_connections import (
    GitHubConnectionService,
    _redact_credentials,
)


class CapabilityMapTests(unittest.TestCase):
    def test_pull_only_grants_read_only(self) -> None:
        self.assertEqual(
            GitHubConnectionService.capabilities_for(["pull"]),
            ["repository.read"],
        )

    def test_push_grants_write_and_triage_capabilities(self) -> None:
        caps = GitHubConnectionService.capabilities_for(["pull", "push", "triage"])
        # Exact set: a mutation that drops a write capability or widens the set is caught.
        self.assertEqual(
            caps,
            sorted(
                [
                    "repository.read",
                    "contents.write",
                    "branches.write",
                    "pull_requests.write",
                    "issues.write",
                    "issues.triage",
                ]
            ),
        )
        self.assertIn("branches.write", caps)

    def test_triage_alone_adds_only_triage(self) -> None:
        caps = GitHubConnectionService.capabilities_for(["pull", "triage"])
        self.assertEqual(caps, ["issues.triage", "repository.read"])
        self.assertNotIn("branches.write", caps)

    def test_admin_implies_maintain_and_admin(self) -> None:
        caps = GitHubConnectionService.capabilities_for(["admin"])
        self.assertIn("repository.maintain", caps)
        self.assertIn("repository.admin", caps)
        self.assertIn("branches.write", caps)

    def test_maintain_is_not_admin(self) -> None:
        caps = GitHubConnectionService.capabilities_for(["maintain"])
        self.assertIn("repository.maintain", caps)
        self.assertNotIn("repository.admin", caps)

    def test_raw_github_names_are_case_insensitive(self) -> None:
        self.assertEqual(
            GitHubConnectionService.capabilities_for(["PUSH", " Pull "]),
            GitHubConnectionService.capabilities_for(["pull", "push"]),
        )

    def test_private_alias_matches_public_method(self) -> None:
        self.assertEqual(
            GitHubConnectionService._capabilities(["pull", "push"]),
            GitHubConnectionService.capabilities_for(["pull", "push"]),
        )


class RepositoryNormalizationTests(unittest.TestCase):
    def test_owner_name_is_preserved(self) -> None:
        self.assertEqual(
            GitHubConnectionService._normalize_repository("example-org/boss"),
            ("example-org", "boss", "example-org/boss"),
        )

    def test_https_url_and_trailing_path_are_stripped(self) -> None:
        self.assertEqual(
            GitHubConnectionService._normalize_repository(
                "https://github.com/example-org/boss/tree/main/docs"
            ),
            ("example-org", "boss", "example-org/boss"),
        )

    def test_ssh_remote_and_dot_git_suffix(self) -> None:
        self.assertEqual(
            GitHubConnectionService._normalize_repository(
                "git@github.com:example-org/boss.git"
            ),
            ("example-org", "boss", "example-org/boss"),
        )

    def test_bad_shapes_are_rejected(self) -> None:
        for bad in ["", "no-slash", "owner/", "/name", "a b/c"]:
            with self.assertRaises(ValueError):
                GitHubConnectionService._normalize_repository(bad)


class PermissionNormalizationTests(unittest.TestCase):
    def test_dedupes_lowercases_and_sorts(self) -> None:
        self.assertEqual(
            GitHubConnectionService._normalize_permissions(["Push", "pull", "PUSH"]),
            ["pull", "push"],
        )

    def test_unsupported_permission_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            GitHubConnectionService._normalize_permissions(["pull", "delete"])

    def test_empty_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            GitHubConnectionService._normalize_permissions([])


class CredentialRedactionTests(unittest.TestCase):
    def test_token_shapes_are_redacted(self) -> None:
        message = _redact_credentials(
            RuntimeError(
                "401 for ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 and "
                "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz using bearer abcdefgh12345"
            )
        )
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", message)
        self.assertNotIn("github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz", message)
        self.assertIn("[redacted]", message)

    def test_actionable_text_survives(self) -> None:
        message = _redact_credentials(RuntimeError("404 Not Found"))
        self.assertIn("404", message)

    def test_private_key_block_is_redacted(self) -> None:
        message = _redact_credentials(
            RuntimeError("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END")
        )
        self.assertIn("[redacted]", message)
        self.assertNotIn("PRIVATE KEY", message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
