# Conversation stream authority

This describes the RC9 source behavior for Talk, room replies, Summary, and Propose. Earlier installed releases may not contain these protections. It is a source contract, not evidence of a successful call to your provider.

Each admitted reply uses the current signed-in principal and conversation ownership. Before the pinned model adapter is contacted, BOSS commits a budget reservation, a job with a renewable lease, a model lease, and a unique invocation receipt. A legacy display actor does not supply job authority. A room admits one speaker at a time and stops after an uncertain outcome.

Text replies stream live. A reply has a 1 MiB UTF-8 accumulation limit, usage evidence has a 64 KiB limit, and the configured turn deadline is bounded between 5 and 600 seconds. These local limits do not guarantee that a remote provider cancels an accepted request. The heartbeat stops at the deadline; expired or reclaimed authority cannot settle or authorize another attempt. Adapter and underlying CLI cancellation remain provider-specific qualification boundaries.

The selected source determines whether a call is paid or included. A paid reply needs typed nonnegative cost evidence or valid tokens for a known price. Missing or malformed evidence is unresolved, not zero cost. The reservation is an admission estimate, not a hard limit on the provider's final charge. Successful settlement records the actual cost once; included capacity records zero cash and retains its equivalent value.

An interruption, unknown provider exception, expired authority after contact, output overflow, or failed settlement preserves the unresolved receipt and financial hold. Positive outstanding liability remains across reservation expiry and accounting-period changes until reconciliation. A visible error and decision identify work needing attention. A demonstrably unused attempt can be released only with the matching job and model authority.

Clients should send one `Idempotency-Key` for one press and reuse it for transport retries. The three POST endpoints ending in `/turns`, `/summary`, and `/propose` bind that key to the authenticated principal and exact request. A concurrent retry waits/refuses with HTTP 409. A fully produced, bounded response replays its exact bytes without a second turn or model call. A changed request with the same key is refused. Current permissions and conversation ownership are rechecked before replay.

A disconnected, oversized, or incompletely closed HTTP producer keeps its durable key held; headers alone never certify completion. HTTP uncertainty and provider uncertainty are separate: a provider cost may be durably settled while response delivery still needs reconciliation. A new key is a new intent, not evidence that an earlier provider call was unused.

Summary and Propose may validate a structured reply after the model cost is settled. An invalid proposal does not start work or erase that charge. Starting an accepted proposal remains a separate governed action.
