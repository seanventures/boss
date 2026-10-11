# Returning to work

The RC9 Command Center shows a compact **Since your last visit** section when recent recorded work is newer than your previous visit. Open a record to inspect the exact attempt, its output and its history. Failed work opens the existing run for review; opening it does not retry, approve or spend.

The brief shows up to three recent records from the existing history window. It is not a complete activity log. A recorded completion or opened pull request does not mean the checks passed, the latest revision is accepted, or the work was deployed. Review the selected attempt in Factory or Workflows for those facts.

If BOSS cannot read its current state, the brief labels the last available records as stale. It does not report that work has stopped or finished merely because the connection was lost. Follow the existing run's controls and confirm its recorded state before deciding how to continue.

BOSS remembers only the last viewed snapshot timestamp in your browser, separately for each organization, project and signed-in viewer. It records that timestamp while the Command Center is visible and its current read succeeds. No task text or results are stored there. A new browser, cleared browser storage or blocked browser storage starts without a previous-visit timestamp.

This section is available in the RC9 source candidate. Use [result review](RESULT_REVIEW.md) to record whether a delivered revision is useful, and the [operator guide](GETTING_STARTED.md) when its state or evidence needs investigation.
