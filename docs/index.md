<section class="boss-hero">
  <div class="boss-hero__inner">
    <p class="boss-hero__eyebrow">Business Operating System Sentinel</p>
    <h1>BOSS<br><span>Every effect accounted for.</span></h1>
    <p class="boss-hero__lede">Turn a CEO command into governed work, validated changes, and an audit trail that explains what happened and why.</p>
    <p class="boss-hero__actions">
      <a class="boss-primary-link" href="features/">Browse capabilities</a>
      <a href="product/PRD-v2.0/">Read the product model</a>
    </p>
    <div class="boss-control-loop" aria-label="BOSS governed execution loop">
      <div><small>01</small><strong>Command</strong></div>
      <div><small>02</small><strong>Plan</strong></div>
      <div><small>03</small><strong>Budget gate</strong></div>
      <div><small>04</small><strong>Lease</strong></div>
      <div><small>05</small><strong>Receipt</strong></div>
    </div>
  </div>
</section>

## Control is the product

BOSS is built around a few non-negotiable controls:

- every worker action uses a lease with a TTL and heartbeat;
- every spendy action reserves budget before execution;
- every external effect produces a deduplicated Receipt;
- every provider write is represented by a ChangeSet and ResourceReceipt; and
- every failure becomes a retry, deadletter, or explicit decision.

## Start here

- Browse the [feature catalog](features/index.md) generated from the product
  traceability ledger.
- Read the [product requirements](product/PRD-v2.0.md) and
  [technical specification](product/TECHNICAL_SPEC-v2.0.md).
- Operate the reactive substrate with the [webhook runbook](operations/WEBHOOKS.md).
- Review the [roadmap](product/ROADMAP-v2.0.md) for planned and blocked work.

The feature pages are regenerated from `docs/product/TRACEABILITY.md`. A CI
drift check and strict MkDocs build must pass before documentation can merge.
