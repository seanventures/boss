# BOSS Community 1.0.0-rc.10.34 candidate

RC10.34 lets BOSS agents get on with development work without asking you. Production still waits for a person. This is the release for enterprise design-partner testing.

This is a test candidate. Cutting the tag does not deploy a host or publish an image.

## What is better for you

**Development runs on its own, production asks**

- In development, BOSS now decides most approval cards itself, including:
  - spec approval and plan sign-off;
  - merges into branches that don't ship to production;
  - deploys to staging, to this machine or to other non-production targets;
  - routine tool calls;
  - self-fix installs;
  - re-running a failed Codex write check.
- It still asks a person for:
  - production deploys, merges into a branch that ships to production, and production infrastructure;
  - passwords and tokens;
  - spend over budget plus the top-up;
  - new grants of authority or wider network access;
  - decisions that need two people;
  - release approval;
  - activating a skill, changing an agent's prompt, and hiring an agent;
  - any stop a person set, such as "Review the plan first".
- Every automatic decision is recorded as "Decided by <agent> under dev autonomy". The **Work** tab lists them under **Decided for you**, latest first. Each one has **Undo** where an undo exists, or says why it can't be undone, and a **Looks good** tick.
- **Govern › Where people decide › Autonomy** sets development to **Agents act**, **Act within cap** or **Ask me**, along with the top-up per item and per day. A project can only be stricter than the organisation. Production, unknown remote servers and explicit stops are locked. **Setup** shows the setting in one line.

**What counts as production**

- One rule decides, everywhere in BOSS:
  1. A tier a person set on a target wins.
  2. Otherwise a production word in the environment name means production.
  3. Otherwise a target on this machine means development.
  4. Anything else, such as an unlabelled remote server, is production.
- Changing a target's tier, or where a branch ships, needs the production permission.
- A merge counts as production when its branch is the repository's default branch and that branch ships to production, or is `release/*`, `prod-*`, `live-*` or a deploy branch. Local repositories never count.

**Never merges over red checks**

- Dev autonomy never merges a change whose checks are failing. That stays a person's decision, with a written reason.
- In a change set across several repositories, a part whose pull request was opened with failing checks no longer counts as ready. The set card says which checks failed and asks a person, and **Merge all N with checks failing** takes a reason. Drafts are marked ready before anything merges; if one can't be, nothing merges.

**Decision cards**

- Approving a storage card now carries it out: deleting old backups or saving a retention rule. Before, it started nothing.
- A test now fails the build if a decision card's buttons don't do what they say.

## Upgrade notes

- `boss upgrade` / `remote.sh update` as usual.
- **Install tier:**
  - The installer records whether an install is development or production.
  - A macOS install on localhost is development.
  - Any other install is production unless it is installed with `--install-tier dev`.
  - `remote.sh install` and `remote.sh update` write production unless you pass `--install-tier dev`.
  - If the tier is unset, BOSS treats itself as development only when its own address is on this machine.
- Cards already waiting before the upgrade stay with people.
- To keep the old behaviour, set **Govern › Where people decide › Autonomy › Development** to **Ask me**.

## Known issues

- The failure-message rewrite and the shrinking known-failures lists (PR #304) are not in this release or in rc10.33, although the rc10.33 notes first said they were. They ship in rc10.35.
- On a development install, deleting old backups is decided automatically and cannot be undone. A production install asks.
- Approval delegates cannot yet answer development deploys.
- The rc10.33 known issues still apply: Marketplace org packs with project-specific custom steps, two people publishing at once, the Library header wrapping at 1440px, Approvals cards missing run numbers, spec cards without Request changes, and ⋯ on its own line in the phone-width editor bar.

## Checks

- Every PR had an independent review, and every review finding was fixed before merging. The first review found three serious gaps in the autonomy rules, and they were closed before merge: every install counting as development, decisions that need two people, and stops meant for a person.
- Every PR passed the local merge gate, which runs the P0 tier with no new failures against main. The P0 tier now includes the autonomy rule tests and the decision-card test.
- End-to-end gates on the laptop install: plumbing 20 of 20, engines 21 of 21.
- The laptop install was walked in a browser: Setup, Govern › Where people decide and Work, at 1440, 1024, 768 and 400 px, with no horizontal scroll and no page errors. The walk found the Setup line missing on the current Setup layout, which is fixed in this release.

