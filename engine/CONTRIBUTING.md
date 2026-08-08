# Contributing to BOSS Engine

Thank you for improving BOSS Engine. Until the BOSS-293 public-flip gates are
complete, this repository is private and contributions are coordinated through
the design-partner program.

## Before opening work

1. Read the current README and the relevant product or implementation record.
2. File or locate an issue before starting; describe the user outcome,
   acceptance criteria, risk, and test plan.
3. Keep changes focused. Do not add credentials, customer data, private host
   names, or generated build output.
4. Treat external effects as governed work: do not bypass approvals, budget
   reservations, receipts, dedupe keys, or provider adapter boundaries.

## Pull requests

Every pull request should include:

- the linked issue and a concise behavior-oriented summary;
- tests and validation actually run;
- migration, compatibility, or security notes where relevant;
- evidence for changed state-machine transitions or external-effect behavior;
- an explicit description of any remaining manual or external release step.

Small, reviewable changes are preferred. Do not merge your own PR when the
repository policy requires independent review.

## Development expectations

Use Python 3.11 or newer and keep the standalone boundary intact: the engine
must not require the BOSS HTTP API or web console simply to import or run its
supported CLI/MCP paths. Run the targeted tests for your change and the
repository's applicable checks before requesting review.

## License and contribution terms

The BOSS OSS-boundary and contribution-terms decision is still open in BOSS-300.
Do not add a CLA, DCO, or copyright-assignment requirement until product and
legal approve the model. Once decided, this document must be updated before
accepting public contributions.
