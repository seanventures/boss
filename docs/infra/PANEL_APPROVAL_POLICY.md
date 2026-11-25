# Automatic panel PRD approval

To let a reviewed PRD continue to a governed build without a second human
approval, set the installation environment:

```dotenv
BOSS_MEETING_PRD_AUTO_APPROVE=true
```

Restart the API and worker to load this policy. The setting already exists in
BOSS; no broad permission change is required. It records a decision with the
auto_approval actor rather than bypassing the decision service.

An explicit request to review the plan first still takes precedence. A PRD that
changes the requested stack still stops for review. Merge, production deployment,
architecture and security-specific approvals retain their own policies.

Changing this setting affects newly finalized panels; it does not retroactively
approve every pending decision. Review already-waiting PRDs separately and use
the normal decision transition for those explicitly authorized by the operator.

Unset, the existing default stays unchanged: PRD approval is manual when identity
enforcement is enabled. Set false to require human approval for ordinary panels.
