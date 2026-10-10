# Review a result

These controls describe RC9 source behavior. Earlier installed RC8 versions may
not include the result-review card.

When a Factory build completes, open its cockpit and find **Review the result**.
Review the delivered change against its acceptance criteria, then record whether
it is useful or describe a bounded revision.

## Inspect the recorded delivery

- Open the **Delivered revision** link to inspect the saved pull-request commit.
  **Open pull request** opens the PR, whose current head may have moved.
- Compare the result with the listed acceptance criteria. A completed build is
  not a statement that the result is useful to you.
- Expand **Evidence and receipts** to see the checked revision, test-report link,
  failed gates, test commands, and delivery receipt identity and status.
- Read the cost figures separately: observed cost receipts, recorded estimates,
  and outstanding reservation holds. Included subscription or local usage is shown
  separately as **Included value**, not cash paid. **Unknown** does not mean zero. **Incomplete**
  means recorded uncertainty or outstanding holds remain. These records may change
  after reconciliation; they are not a provider invoice or a final-cost guarantee.

This view reads saved BOSS evidence. It does not contact the repository provider
to check the current remote head. Checks with no report are **not recorded**;
checks without a valid recorded commit are **unbound**. A checked commit that
differs from the delivered commit is **invalidated**. Recorded failures, skipped
checks, and checks not run remain visible.

## Accept as useful

Choose **Accept as useful** to record your assessment of this revision. A note is
optional. BOSS saves the authenticated person's identity, time, outcome, and an
immutable snapshot identifying the delivered commit, evidence, criteria, and costs.

Acceptance records usefulness only. It cannot pass a technical check, waive a
failed gate, approve a merge or deployment, release a reservation, or start more
work. Review any pending merge or deployment decision separately. A person may
find a result useful while its technical checks still block publication.

Only an authenticated person with this build's steering permission can record
the review. A worker, service account, agent, or delegated run cannot supply
human acceptance. A build without a completed delivery, known revision, and
linked successful delivery receipt remains read-only here.

## Request a bounded change

Write the change you want, then choose **Request a change**. BOSS first saves the
review against the recorded revision, then opens the existing **Change it** sheet
with that revision and your note filled in.

Edit **What to change** and **Done when**, inspect the proposal, and choose **Start**
only when you intend to run it. Saving a change request does not propose, dispatch,
or spend on the next build automatically. Starting uses the existing governed
follow-up path and its permissions, budget and execution controls. It creates a
new attempt linked to the earlier build; it does not overwrite the earlier result.
Its working branch may have moved since the reviewed commit.

The saved note remains in **Review history**. **Prepare this change** opens that
note again if you closed the sheet. Opening it still does not start work.

## Changed evidence and retries

If the saved delivery or evidence changes before you submit, BOSS refuses the
stale review. Choose **Reload result**, inspect the updated evidence, then submit
again. Your unsent note stays in the field.

A retry of the same submission returns the existing review and does not add a
second audit event. Later reviews are additional records; earlier reviews keep
their original revision and evidence snapshot and are labelled when the current
snapshot differs.
