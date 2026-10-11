# Historical model and provider exposure

This describes the RC9 source candidate. Earlier releases could expire or release a positive budget reservation while its model invocation remained unfinished. Updating the software does not prove that the provider never accepted that work.

BOSS now holds new paid admission when it can identify a positive expired or released reservation linked to an in-progress model invocation without its matching cost receipt. It keeps the original reservation and lease authority expired or released. It never creates an invoice or revives authority to reconstruct that history.

The Budget status and Govern meter expose `legacy_financial_hold`. It identifies one unresolved reservation, lease, invocation and receipt, labels the original estimate, and links the reconciliation decision when committed. The example estimate is neither a project total nor an actual charge. Ordinary spent, reserved and cap figures keep their existing meaning; they are not permission to bypass this separate hold.

## What remains available

Exactly-zero cash work on the actual included subscription or local source can proceed under its usual permissions, reservation, job and model leases. Both the new model estimate and any reused reservation must be zero. This conservative exception also refuses an included call whose gateway estimate is positive API-equivalent value, even if its cash reservation would be zero; it does not preserve every subscription or local call. A paid fallback, a positive reused reservation, or a source whose zero-cash status is unknown is refused. Existing heartbeat, completion and settlement rules remain unchanged.

Raising a cap, waiting for midnight, restarting, or resolving the displayed decision does not clear unresolved financial evidence. The next scan reopens the same decision while the underlying condition remains. A successful budget/status transaction commits the visible decision. If a refusing caller rolls back its whole transaction, its decision write rolls back too; the durable history still causes the next admission to refuse.

## Reconcile the specific attempt

Record the project and hold IDs, keep the old execution authority revoked, and inspect provider-specific acceptance and cost evidence through an authorized process. The original estimate does not establish the actual charge. Do not reset the reservation to active, retry the invocation, invent a cost receipt, or delete history to remove the warning.

This release adds a model-history admission fence, not a universal reconciliation writer. Accounting-only correction or a documented proof of no effect requires a reviewed provider-specific procedure, canonical authority and an immutable audit. Existing settlement mechanisms retain their existing preconditions; this hold does not make an expired lease valid for completion.

## Discarded provider attempts

An operation that failed before acceptance and one whose response was lost after acceptance can leave the same failed local receipts. BOSS conservatively quarantines both when a positive expired/released reservation links to the same project's ChangeSet, write operation and receipts without complete settlement evidence. A failed status, missing provider reference, generic resolved decision or transport-error message cannot prove that the provider never accepted it. Unexpected historical receipt states also stay held.

Budget status and Govern expose a separate nullable `legacy_provider_hold`: reservation, ChangeSet, operation, resource receipt and base receipt IDs; original reservation status and estimate; optional decision ID; and a fixed explanation. If both model and provider holds exist, both remain visible. Neither example is a total or an invoice. The same positive admission/reuse/start fence and conservative exact-zero included exception apply, with original states and ordinary money totals preserved.

The no-contact exclusion requires the complete server-recorded no-dispatch protocol: matching failed base/resource receipts with `provider_not_dispatched`, the corresponding audit event, and every sibling operation accounted for with no ambiguous attempt. A partial apply cannot use a later no-dispatch result to erase earlier exposure. The settlement exclusion requires an applied ChangeSet with all operations and receipts succeeded, plus its same-project, same-reservation `resource_plane.changeset.apply` cost receipt tied to a receipt of that ChangeSet. The existing protocol settles the whole ChangeSet through one cost receipt; BOSS does not demand one per operation.

Some genuine no-effect historical failures will therefore remain paid-blocked until provider-specific reconciliation can establish the outcome. This release does not add a generic Resume action or an accounting correction writer. Terminal failed model invocations are outside the separate unfinished-model predicate, and missing or erased historical linkage cannot be reconstructed by either scan. See [Provider apply and recovery](PROVIDER_RECOVERY.md). Restored application startup and any external-effect resumption require their separate recovery hold and reconciliation qualification.
