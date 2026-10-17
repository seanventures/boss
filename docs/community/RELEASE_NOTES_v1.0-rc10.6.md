# BOSS Community 1.0.0-rc.10.6 candidate

RC10.6 builds on rc10.5. It is the first candidate with a complete HelpDesk AI run behind it: brief, PRD,
architecture, build, PR, preview, more tests, deploy, monitoring and notify, ending with a working app
on a deploy host. Everything that run needed is in this release. This is a test candidate.

## What changed

### Deploys that fix themselves

- **The host is asked first.** Before anything is touched, the deploy checks the machine for every program
  the manifest runs and records what it has (Docker, Node, Python…). A manifest that runs `npm` on a machine
  without Node is refused with that reason, not after the release was half switched.
- **Fix and redeploy.** When a deploy fails for the app's own reason, BOSS starts a repair build on the same
  PR branch with the failure, the log and the machine's facts, then redeploys the new commit. A failure on the
  machine's side (SSH, disk, Docker) still goes to a person, and so does a deployment someone rejected.
- **The build knows the target.** A build that will be deployed is told what the target machine has, so it
  writes the right manifest the first time.
- **Compose apps reach their database.** A `kind: compose` app runs in the same project as the resources BOSS
  provisioned for it, so `res-db` resolves; an older copy of the app under its own project is retired first so
  it cannot hold the port. A failed health check now records the containers' state and the app's own last
  lines.
- **An app name with a space can hold a provisioned secret.**

### Runs that finish

- **A team nobody chose gains the workflow's agents**, including approvers inside parallel branches, so a deploy
  is not refused for "release_manager is not assigned".
- **A failed run keeps its authority for 24 hours**, so retrying a step can finish the run.
- **A retry builds onto the kept work**, never an empty tree, and the secret scan gives the builder one turn to
  fix a finding before blocking.
- **The app's folder comes from the app's record.** In a repository with several apps, a planner never treats
  the shared root as the app; with no evidence it refuses instead of guessing.
- **An app start keeps its own output** and gets the environment the app declares.

### Console

- **App Setup redesigned.** One card per connector (earlier attempts folded), a collapsible row per sign-in
  provider with its steps, callback and keys, warnings only where an address cannot work, and a Settings tab
  that separates what BOSS manages from your own settings. The Factory row shows a "Set up sign-in" chip when
  something needs you.
- **One Retry on screen.** Retry opens a single panel with an optional note whose example fits the step.
- **An app's health panel opens wide** and expands to the whole page.

## Not yet verified

- Agents recovering on their own across builds and deploys (being built for the next candidate).
- The live per-engine matrix (Codex, OpenRouter, Groq, Ollama) on the release gate.
- Website installer parity.

## Install and upgrade

Select `v1.0-rc10.6` explicitly with the installer's `--ref` option, from a checkout of that same tag. See the
[Quickstart](QUICKSTART.md).

Before upgrading an existing install, take a complete protected backup (`boss upgrade` does this). Changing
the checkout back does not undo database migrations.
