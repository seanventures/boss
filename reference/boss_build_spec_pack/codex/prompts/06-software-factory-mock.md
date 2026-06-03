# Codex Prompt 06 — Software Factory mock workflow

Goal: Implement a mock software factory workflow that creates review packets and mock PR proposals without real GitHub calls.

Context to read first:

- AGENTS.md
- specpack/workflows/ceo_command_to_pr.yaml
- specpack/skills/software_factory.yaml
- specpack/skills/github.yaml
- specpack/evals/software_factory_mock.yaml

Constraints:

- No real GitHub calls.
- Mock PR creation must still go through receipt/dedupe.
- The workflow must create artifacts: PRD, TechDesign, DiffArtifact, TestReport, ReviewPacket.

Implement:

1. Job kind `software_factory.mock_run`.
2. Artifact creation helpers.
3. Mock GitHub create PR adapter with dedupe receipt.
4. Merge decision card based on review packet.
5. API/worker wiring so a CEO command can trigger mock software factory after planning.

Done when:

- Smoke eval can submit a command and receive a mock PR decision.
- Duplicate mock PR effect reuses receipt.
- Review packet appears in task artifacts.
