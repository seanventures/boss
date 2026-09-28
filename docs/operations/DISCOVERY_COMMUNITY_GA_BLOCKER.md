# Community GA feature blocker: Discovery research

Discovery research is unavailable in Community as of this candidate. The
`POST /v1/discoveries` and `POST /v1/discoveries/{run_id}/deepen` routes return 503
after project authorization. The MCP and engine `create_discovery` paths reject
before creating a run or launching a model call. Existing dossiers remain
readable, and completed outcomes can still be promoted through their separately
budgeted meeting path.

The current non-Community research service launches a daemon thread from the API process.
Direct API and MCP starts had no model `BudgetReservation`; the orchestration
path reserved once for an entire run but had no durable worker lease. On process
restart, research resumed from its first stage, which could repeat already-paid
model calls. A row timestamp heartbeat did not establish ownership of the work.

Community startup now marks any legacy `running` Discovery row failed and records
a blocking decision to reconcile possible spend and receipts. It does not replay
the model call or silently release a reservation whose actual spend is unknown.

To clear this gate, enqueue Discovery through the durable worker lease path,
reserve before each model effect, settle actual cost against a receipt, and make
an uncertain call block for reconciliation instead of replaying automatically.
Prove budget denial makes zero model calls, a worker crash cannot duplicate an
effect, retries and deadletters are visible, and a completed run has matching
budget, receipt, and event evidence. Until then, do not present Discovery
research as available in Community GA. Non-Community editions retain the legacy
execution path and require separate qualification before release.
