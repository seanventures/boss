# Community GA feature blocker: live investigations

Live debugging investigations are unavailable in Community, including its
`oss` and `local` edition aliases. Unknown edition values also fail closed.
New `investigate` calls stop before creating
an investigation or queueing a job. The worker endpoint and direct service
retire legacy queued or running rows before a Claude CLI call. A legacy running
row also creates a hold-default decision to reconcile uncertain earlier model
spend or host effects. Past investigation results remain readable.

An investigation can invoke the Claude Code CLI for many turns without a
`BudgetReservation` or settled model receipt. Fix mode can reach a host shell,
write SQL rows, or invoke BOSS operations through a sandbox token without the
required ChangeSet, approval, and ResourceReceipt lineage. Community now
rejects the host and operation callbacks and refuses SQL writes for legacy
tokens. Read-only SQL is constrained to a PostgreSQL read-only transaction.

To clear this gate, run investigations only through a validated worker lease,
reserve before each model effect, settle actual usage against a receipt, and
reconcile uncertain calls without automatic replay. Route host and database
changes through approved, receipted operations. Prove that budget denial,
expired leases, and callback retries cause zero external effects. Non-Community
execution retains its earlier behavior and requires separate qualification.
