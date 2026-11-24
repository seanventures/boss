# BOSS Community 1.0.0-rc.10.33 candidate

RC10.33 is the release for enterprise design-partner testing. It brings a Marketplace for workflow packs, standard workflows you can edit in place, an Approvals screen you can read at a glance, a Work panel that shows where each piece of work stands, and a set of fixes from enterprise testing. These fixes cover agent capacity, disk reclaim on Colima, report previews, new work landing on the right app, and repairs that stop at known blockers.

Many of the enterprise fixes came from an enterprise contributor. Their patch was adopted, reconciled with main and reviewed. Thank you.

This is a test candidate. Cutting the tag does not deploy a host or publish an image.

## What is better for you

**Marketplace for workflow packs**

- **Workflows › Library › Marketplace ›** lists packs. BOSS ships **Banking Data Quality** (three DQ workflows) and **SRE / Helpdesk** (Incident Response).
- New projects no longer get these packs by default. **Install** adds a pack to **Yours**, marked "from Marketplace · vX", never to Standards. The install confirm shows what the pack adds, what it needs, and which agents it brings.
- Updates:
  - An unedited copy updates by itself.
  - An edited copy is never overwritten. **Review update** offers to take the update and keep your changes, take the new one only, or skip it for now.
  - Running work keeps its version.
- **Uninstall** is refused while a run is going. A workflow that has run keeps its history.
- **Publish to Marketplace** shares any workflow under Yours with your organisation, with a version and publisher. Other projects are offered the update rather than having it forced on them. Public community publishing is coming later.
- Projects that already had the DQ or Incident Response workflows keep them unchanged. They now show under Yours as "from Marketplace · v1.0".

**Standard workflows are editable**

- Edit a standard workflow and save it in place. The workflow shows **Modified**, and **Changes from standard** lists what you changed.
- **Reset to standard** returns to the shipped version and keeps your edits in history. **Save as your copy** still works.
- When BOSS ships a newer standard, your edited one is never overwritten. You are offered the update, with a clean merge when there are no clashes.

**Approvals you can read**

- The tabs are **Needs you**, **Expired** and **Resolved**. "Also waiting: N in M other projects ›" lists the other projects by name.
- Each card has three lines: the action, one status line (for example ✓ Ready · 9 issues found, all fixed · Checks ›), and the answer buttons with Workflow / Change links. Detail sits behind **Details**.
- The workflow run screen shows the same card.

**Work panel**

- Each piece of work shows four stages, **Plan · Build · Ship · Live**, and Watch live opens the screen for the stage that is running.
- The tabs are Needs you / BOSS working / Done / Stopped.

**From enterprise testing**

- **Setup › Agent capacity** sets **Job slots** and **Heavy builds** separately. Lowering capacity drains slots and never kills running work. Heavy builds back off under CPU or memory pressure.
- **New work** on a project with several apps asks you to **Choose an application** or **Create a new app**. It no longer builds on an empty default branch. Preview repair builds on the app that produced the preview.
- Self-repair no longer counts time spent waiting in the queue against its execution limit.
- A Codex writer whose last check said it cannot write is stopped before it starts, with a message that points to **Setup › Models › Re-check**. It no longer launches repair after repair.
- A model that answered with invalid output is no longer reported as a disconnected model.
- An app can declare a generated HTML report, which BOSS serves as an isolated preview. Paths and symlinks are checked, and one file is served.
- Storage status reports what the host actually shows. Cleanup says when it is exhausted and what to free by hand. The host helper can reclaim unused Colima disk space through the signed dry-run and execute path.
- With the engine sandbox enabled, the AppArmor launcher enters the engine sandbox profile before setting no-new-privileges. Codex writers then work under the hardened profile.
- Failure messages in these areas now say what happened and what to do next.

**Correction (2026-11-24): not in this release**

- The earlier version of these notes listed about 65 rewritten failure messages, a test that fails the build on a vague failure message, and per-suite known-failures lists that only shrink. That change (PR #304) passed its checks but was never merged, so it is not in rc10.33 or rc10.34. It is being brought up to date and will ship in the next release.
- The failure-message fixes listed under **From enterprise testing** above are in this release.

**Security**

- Every id the API accepts is now checked against its project or organisation (86 id names were unchecked). Records such as change sets, datasets, alerts, documents and voice transcripts can no longer be read from another project by id. A model provider sign-in in progress is confined to its organisation.
- Activity exports to CSV and NDJSON no longer stop partway through.

## Upgrade notes

- `boss upgrade` / `remote.sh update` as usual.
- **Marketplace packs:** existing projects keep their DQ and Incident Response workflows. New projects need **Install** from the Marketplace.
- **Agent capacity:** until you save a value, job slots follow "builds at once" as before. A pinned "builds at once" is now capped at what the host can measure, where up to twice that was allowed before.
- **Report previews** run `python3` in the preview runtime. An image without python3 has not been tested.

## Known issues and what was not verified

- On the live laptop install, editing a standard needs a step-up sign-in code, so that flow was walked end to end on a private test database rather than by us on the live install.
- GitHub Actions does not run for this repository yet. The local merge gate is the gate.
- A Codex writer check that failed is not re-run automatically. Someone presses **Re-check** in Setup › Models. Making this automatic is planned.
- Marketplace:
  - An organisation pack with project-specific custom steps may fail to install in another project.
  - Two people publishing the same pack at the same moment can get a server error.
  - At 1440px, "Marketplace ›" wraps to a second row in the Library header.
- Approvals:
  - Most non-merge cards have no run number on their Workflow link.
  - Spec cards offer Approve and Reject but not Request changes.
- The phone-width editor bar puts ⋯ on its own line.

## Checks

- Every PR: impacted tests on the branch and on main, with no new failures; an independent review; and the local merge gate (schema applies twice, web type-check, Python 3.11 compile).
- End-to-end plumbing gate on the laptop install: 20 of 20 passed. The engines gate (scripted conformance engine, both engine profiles) is updated to the pipeline as it now records review, mutation and must-have follow-ups. It is stricter, and passed 21 of 21.
- The merge gate now also runs the P0 tier, about 13 minutes per PR, and counts only failures new against main.
- Laptop install walked in a browser: Approvals (40 links opened), the workflow editor, Library, Marketplace and the install confirm, and Setup › Agent capacity. No horizontal scroll at 1024, 768 or 400 px.
