# Reuse acceptance checks

RC9 source adds **Use these checks again** to Factory → Outputs → Review the result.
This is a way to carry your own definition of a useful result into another task.
It does not start work, merge a pull request, pass technical checks, or grant future authority.

1. Inspect the recorded delivery and its evidence in [Result review](RESULT_REVIEW.md).
2. Accept the current recorded revision as useful. If its delivery, checks, criteria or cost
   records change, reload and review that new snapshot first.
3. Press **Save accepted checks**. BOSS copies the recorded acceptance criteria into an
   internal project artifact, alongside the source review, exact revision and evidence hash.
   It does not copy the private task brief, review note, code, logs or credential fields.
   The check text is your content: inspect it and remove private details before copying it.
4. **Edit saved checks** to tailor their name and checks. Each save appends a revision;
   another edit or a changed source review stops a stale save. Keep 1–20 checks, each within
   500 characters and 6,000 characters in total. Inspect the text before reusing it.
5. Press **Copy checks for a fresh task**, then paste them into a new task with its own
   context. Review its fresh [work agreement](WORK_AGREEMENT.md) and current budget, consent
   and readiness before explicitly starting it. If clipboard access is unavailable, select
   and copy the displayed checks manually.

Only an authenticated active person with this project's `workflow:create` permission can
save or edit checks. Readers with `workflow:read` can inspect project templates. Agent/run
credentials cannot supply the human save. A template retains its original source provenance
even when its criteria are edited; it describes a starting point, not proof of a later result.
The list shows the 100 most recently saved project templates, with their latest revision.

Saving, editing, listing and copying checks make no model or provider call and create no job
or reservation. A source snapshot becoming stale does not erase a saved revision. You can
still inspect or copy its historical checks, but further edits require the source's current
accepted exact snapshot. Changes requested on the source therefore stop new revisions of
that template; review and save the new accepted result instead.

This source functionality has fixture and mounted-control qualification. Better second-use
results, actual useful provider delivery, and a complete installed human walkthrough remain
separate release qualifications. Human acceptance is a recorded judgment, not independent
measurement of usefulness.
