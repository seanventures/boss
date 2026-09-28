# Community feedback

The GitHub issue is the public record for a Community bug, setup problem, or feature request. Use the bug, installation, or feature form in `.github/ISSUE_TEMPLATE/` when the repository becomes public. For a suspected vulnerability, do not open an issue; email **boss@sean.net** privately as described in `SECURITY.md` in the source checkout.

## How a report becomes a change

1. **Report.** Describe the observed result, expected result, steps, and version. Remove credentials and private data. The issue stays open while a maintainer investigates.
2. **Triage.** Maintainers reproduce bugs, ask for missing detail, and link duplicates. When the signed GitHub App integration is configured, BOSS posts one receipt-backed first response and creates a hold-default decision. Bug and installation reports go to maintainer review. A complete feature request may create a draft plan; that plan has no authority to start work.
3. **Decide.** A maintainer records the outcome on the issue: accepted with scope, needs more information, duplicate, deferred, or declined with a reason. BOSS's internal decision and Plane issue are operational records; internal IDs are not required from contributors.
4. **Build and verify.** An accepted change gets a linked PR. The PR names its tests and any policy, budget, receipt, or migration effects. The GitHub agent may comment on a PR, but it cannot merge one.
5. **Close the loop.** Before closing the issue, a maintainer links the shipped PR or release, says how to verify the result, and invites a follow-up if the original problem remains. Reopen or file a regression if the evidence does not match the report.

The GitHub App is optional. Without it, maintainers perform the same steps manually. An operator who enables it must configure a signed webhook and review the App's permissions before allowing automated replies.

## What to expect

- Opening an issue does not authorize model spend, code execution, a provider write, deployment, or merge.
- A bot reply is an intake acknowledgement, not a resolution or promise to ship.
- BOSS does not collect usage telemetry from a self-hosted Community install for this loop. Contributors choose what they post in GitHub.
