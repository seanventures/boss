# Releasing BOSS Engine

This procedure applies after BOSS-293's public-release gates are approved.
Until then it is a rehearsal checklist only.

## Versioning

BOSS Engine uses semantic versioning:

- **MAJOR** for incompatible public CLI, MCP, schema, or behavior changes;
- **MINOR** for backward-compatible capabilities; and
- **PATCH** for backward-compatible fixes and documentation corrections.

The release version must agree in package metadata, tags, image labels, release
notes, and any published artifact.

## Required evidence before tagging

1. The hard gates in [COMMUNITY_READINESS.md](./COMMUNITY_READINESS.md) are
   complete and evidence is recorded in BOSS-293.
2. The extracted private repository has a clean working tree and protected
   default branch.
3. Run the approved engine validation matrix, including package installation,
   engine tests, the engine verification script, and Docker image build.
4. Run the approved full-history secret scan plus dependency/license audit.
5. Review release notes for accurate community-versus-commercial boundaries
   and known limitations.
6. Obtain the required independent release approval.

## Publish and verify

1. Create the annotated version tag from the reviewed commit.
2. Build and publish only to the approved package/image registries.
3. Record immutable package/image digests and the source commit in BOSS-293.
4. Install the published artifact in a clean environment, run the documented
   local bootstrap/MCP proof, and attach the evidence.
5. Publish release notes, support/escalation route, and rollback guidance.

If a required validation fails, publication is stopped. If publication must be
withdrawn, preserve the reason and artifact identifiers in the release record;
do not overwrite the historical evidence.
