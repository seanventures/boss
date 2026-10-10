# Historical model exposure

This describes the RC9 source candidate. Earlier releases could expire or release a positive budget reservation while its model invocation remained unfinished. Updating the software does not prove that the provider never accepted that work.

BOSS now holds new paid admission when it can identify a positive expired or released reservation linked to an in-progress model invocation without its matching cost receipt. It keeps the original reservation and lease authority expired or released. It never creates an invoice or revives authority to reconstruct that history.

The Budget status and Govern meter expose `legacy_financial_hold`. It identifies one unresolved reservation, lease, invocation and receipt, labels the original estimate, and links the reconciliation decision when committed. The example estimate is neither a project total nor an actual charge. Ordinary spent, reserved and cap figures keep their existing meaning; they are not permission to bypass this separate hold.

## What remains available

Exactly-zero cash work on the actual included subscription or local source can proceed under its usual permissions, reservation, job and model leases. Both the new model estimate and any reused reservation must be zero. This conservative exception also refuses an included call whose gateway estimate is positive API-equivalent value, even if its cash reservation would be zero; it does not preserve every subscription or local call. A paid fallback, a positive reused reservation, or a source whose zero-cash status is unknown is refused. Existing heartbeat, completion and settlement rules remain unchanged.

Raising a cap, waiting for midnight, restarting, or resolving the displayed decision does not clear unresolved financial evidence. The next scan reopens the same decision while the underlying condition remains. A successful budget/status transaction commits the visible decision. If a refusing caller rolls back its whole transaction, its decision write rolls back too; the durable history still causes the next admission to refuse.

## Reconcile the specific attempt

Record the project and hold IDs, keep the old execution authority revoked, and inspect provider-specific acceptance and cost evidence through an authorized process. The original estimate does not establish the actual charge. Do not reset the reservation to active, retry the invocation, invent a cost receipt, or delete history to remove the warning.

This release adds a model-history admission fence, not a universal reconciliation writer. Accounting-only correction or a documented proof of no effect requires a reviewed provider-specific procedure, canonical authority and an immutable audit. Existing settlement mechanisms retain their existing preconditions; this hold does not make an expired lease valid for completion.

Failed legacy provider receipts are a separate ambiguity: an operation that failed before acceptance and one whose response was lost after acceptance can leave the same local failure record. A model-history scan cannot distinguish those cases or recover receipts erased by an older database snapshot. See [Provider apply and recovery](PROVIDER_RECOVERY.md). Restored application startup and any external-effect resumption require their separate recovery hold and reconciliation qualification.
