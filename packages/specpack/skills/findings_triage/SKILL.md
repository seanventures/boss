---
name: findings-triage
description: How the Acceptance Agent matches a finding to the open findings it repeats before calling it new, and words a finding so it can be tracked to a fix and proved fixed. Nothing is filed by the agent; BOSS files from its verdict.
---

# Findings triage

Every real problem in a build lineage is tracked once, whoever found it: you, the in-build
acceptance check, or the code, security and consistency reviewers. You see the open findings
(`F-<n>: title — path:line`) in your brief.

## Same, or new?

- Decide by what the problem IS, not by its words: "no Makefile" from a reviewer and "make
  run-staging fails: no rule to make target" from your own run are the same problem when the
  missing target is the cause.
- Same as an open finding: write `[same as F-<n>]`. Name only an id that is in your list; BOSS
  checks it, and a made-up id is ignored.
- A contract item that is not met is already tracked by its item id (AC-<n>); do not repeat it
  as a FINDING.
- Otherwise `[new]`.

## Wording a finding

- Severity: BLOCKER (the work cannot be used or is unsafe), MAJOR (a promised behaviour is
  missing or wrong), MINOR (works, but rough), NIT (style).
- Where: `path:line`, or the exec id of the command that showed it.
- What is wrong, in one sentence a person can act on.
- How to prove it fixed: the command or request, and what it must print.

## What you never do

File, comment on or move a ticket; edit the branch; mark anything fixed without a command that
shows it fixed in this session.
