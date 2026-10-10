# Provider apply and recovery

This describes the RC9 source candidate. Earlier installed releases do not have this apply boundary. It is a controlled-provider qualification, not a claim that every provider supports exactly-once recovery.

A generic ProviderOps ChangeSet now commits its applying state, unique Receipt, ResourceReceipt and reconciliation decision **before** entering a provider adapter. Its existing project permission, canonical actor, approval, account restrictions, ownership checks and active sufficient budget still apply. Worker-owned operations use their existing leased owner protocol; a supplied job ID cannot confer that authority. SSH app and stack deployments retain their separate phased owner.

Current project permission is checked again after that claim commits. For an operation with a surface-ownership guard, the current project, surface, environment, owner, version, active status and expiry must still match immediately before dispatch. The ownership row remains locked through that adapter invocation and its result transaction; an earlier deduplicated preflight alone is not authority.

When all operations return and their evidence commits, the ChangeSet becomes applied and the reservation settles at the existing estimate. That estimate is not a provider invoice or a cap on recurring infrastructure charges. The reconciliation decision resolves. Repeating the same ChangeSet returns its recorded outcome without another provider call.

## When the outcome is uncertain

A timeout, lost response, process loss, partial apply or failure recording the result can leave an external resource behind. The ChangeSet remains applying or needs reconciliation, its receipt records the attempted operation, and its positive budget reservation stays held. Expiry revokes new execution authority; it does not prove that nothing happened. Another apply request returns the held state and decision without dispatching again.

If the live caller catches a refusal before entering **any** adapter, it can prove no provider call occurred. It records that non-action and releases the unused hold. A crash before contact cannot make that claim because the surviving process cannot know how far execution reached.

For an unresolved apply:

1. Keep the original executors fenced. Record the ChangeSet, operation, receipt, provider account and reservation IDs from the pending decision and receipt history.
2. Inspect the provider through an authorized read-only process. Match resource identity, request timing and intended configuration against the receipt. A name match alone is not proof of unique identity or non-acceptance.
3. Preserve provider response or inventory evidence and any actual cost evidence. If the outcome remains unknown, keep the hold. Do not create a replacement ChangeSet as a shortcut around it.
4. Reconcile the specific operation and accounting under the provider's reviewed recovery procedure. This release does not supply a universal reconcile-and-resume command. Approving or resolving the visible decision by itself does not authorize replay, clear the ChangeSet, or establish that an effect was absent.

Do not delete receipts, reset an applying ChangeSet to approved, release an uncertain reservation, or retry a raw provider command to make the warning disappear. A separately reviewed rollback can itself have an external effect and needs its own authority and receipts.

## Restoring an older database

A backup taken before the effect can erase its later local receipt while the provider resource survives. The committed apply boundary alone cannot detect that older history. Database/key restoration and safe resumption of external work are separate qualifications.

Keep restored application services stopped until the restore startup-hold contract and a provider-specific reconciliation plan have been reviewed. An API process can perform external actions even while workers are stopped. Earlier source/image pairs must not be treated as guarded merely because a new helper restored their data. Offline data inspection does not authorize normal application startup.

The controlled regression uses real PostgreSQL transactions and a provider witness outside the database. It proves that admitted claims survive caller rollback and refuse repeated contact, ambiguous liability remains held, concurrent requests contact once, and current API permissions are enforced. It does not establish live-provider idempotency, universal disaster recovery, or safe automatic resumption from an arbitrary older snapshot.
