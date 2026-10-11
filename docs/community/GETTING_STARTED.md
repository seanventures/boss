# Getting started

This guide walks through installation and a small governed build. Follow the checks at each stage; starting a run does not mean its output is ready. The path is:

1. Install BOSS.
2. Sign in to Mission Control.
3. Connect a model.
4. Connect a GitHub repository.
5. Create your first project.
6. Follow it down the governed path: panel, PRD, approval, code, tests.
7. Review the pull request and run the software.

Plan on an hour or more. The install takes several minutes. On a machine without a GPU, a local model needs tens of minutes for the panel alone.

The command output and messages quoted below were recorded on a fresh Ubuntu 24.04 install (8 CPUs, 16 GB of RAM, no GPU) with a local Ollama model and no paid API key. Steps 4, 6, and 7 depend on GitHub. The notes in those steps say which parts you will see for the first time on your own repository. This historical transcript proves installation and the recorded local-model stages; it is not an end-to-end RC8 qualification or a promise that every repository and model will complete a build.

## What you need

- An Ubuntu 22.04 or 24.04, or Debian 12 or 13, machine with `curl` and `sudo`. macOS with Docker Desktop also works; see the [Quickstart](QUICKSTART.md#before-you-begin) for the full list.
- About 100 GB of working disk capacity is recommended, not a hard minimum; actual needs vary. The installer builds one service image at a time and warns below about 80 GB free. Install files and Docker images can live on different filesystems. Docker Desktop or remote-daemon free space may be unknown to the installer; check that Docker store separately.
- A GitHub account and a repository that BOSS is allowed to push to. BOSS writes code on a branch and opens a pull request. It does not build into a local folder, so step 6 cannot finish without a repository.
- A model. If you have no API key or subscription, the installer can set up a free local model with Ollama.

## 1. Install BOSS

Run one command:

```bash
curl -fsSL https://boss.sean.network/install.sh | bash
```

The first run installs Docker if it is missing, then builds the BOSS containers. That takes several minutes. Reruns are faster.

During the install you are asked about models. If you have no API key, Codex login, Claude Code login, or Grok login, say yes to the free local model. It installs Ollama and pulls `qwen2.5-coder:7b`.

When it finishes, the installer adds a `boss` command to your `PATH`.

**Check that it worked:**

```bash
boss status
```

```text
Install   /home/you/boss
Release   ...
API       ✓ healthy at http://127.0.0.1:8000

Services
NAME                      ...   STATUS
boss-community-api-1      ...   Up About a minute (healthy)
boss-community-db-1       ...   Up 2 minutes (healthy)
boss-community-web-1      ...   Up 2 minutes
boss-community-worker-1   ...   Up About a minute
```

Then run the checkup:

```bash
boss doctor
```

```text
BOSS checkup  (http://127.0.0.1:8000)

  ok    containers: 4 running (db, api, worker required; web and secrets are optional)
  ok    api: /health answers on http://127.0.0.1:8000
  warn  console → API: http://127.0.0.1:8000 uses loopback; forward both console :3000 and API :8000 over SSH before opening it in a browser
  ok    models connected: native
  warn  chat: not asked (run with --probe to prove a model actually answers)
  warn  repository: not connected — builds cannot open a pull request
  ok    host: scheduling BOSS normally
  warn  config: could not compare against a stock install

  BOSS is ready.  Mission Control: http://127.0.0.1:3000
```

`BOSS is ready.` means the install worked. The `warn` lines are expected at this point:

- **console → API** is a reminder for step 2 if BOSS runs on another machine.
- **chat** is checked in step 3.
- **repository** is fixed in step 4.
- **config** means the checkup had nothing to compare against.

### If something goes wrong

- **The install stops on disk space or Postgres.** See the Docker Desktop disk note in the [Quickstart](QUICKSTART.md#install) and the sizing table in the [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md).
- **`boss: command not found`.** Open a new terminal so your `PATH` is reloaded, or run `~/.local/bin/boss`.

## 2. Sign in to Mission Control

Ask BOSS where the console is:

```bash
boss open
```

```text
Mission Control on this machine: http://127.0.0.1:3000
First visit: claim it at /claim. The password from boss password proves it is you.
```

Then show the console password:

```bash
boss password
```

It prints one line: the password. Copy it. It is never written to a log. You use it once, to claim the install. (Or run `boss identity setup-link` for a one-hour claim link instead.)

### Open the console

**On the machine where BOSS is installed:** open `http://127.0.0.1:3000`.

**From another computer:** BOSS listens on `127.0.0.1` only, so forward both ports over SSH and leave the command running:

```bash
ssh -N -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-server
```

Then open `http://127.0.0.1:3000` on the computer that ran `ssh`. If ports 3000 or 8000 are taken on that computer, forward other local ports, for example `-L 3300:127.0.0.1:3000`, and open that port instead.

**For your team, on a domain or private IP:** see [Deploy](hostname.md).

The first time, the sign-in page says **This BOSS has no Owner yet. Claim it.** Click it, paste the password, enter your name and email, and add a passkey (or a password and an authenticator app). You are now the Owner, and you sign in as yourself from then on. Mission Control opens on **Repo and models** while the selected project needs setup, and on **Command Center** once it is ready. You can open Repo and models from Settings at any time.

Invite your team in **Identity › People** ([People and access](PEOPLE_AND_ACCESS.md)). More about sign-in is in [Sign in to Mission Control](CONSOLE_AUTH.md).

### If something goes wrong

- **`Refusing to print a console password outside a terminal`.** `boss password` only prints to an interactive terminal, so that the password does not end up in a log or a pipe. Over SSH, ask for a terminal: `ssh -t user@your-server '~/.local/bin/boss password'`.
- **The browser cannot connect.** Check that the `ssh -N -L ...` command is still running, and that you opened the local port you forwarded.

## 3. Connect a model

See what BOSS found:

```bash
boss connect --summary
```

With only the free local model, the table ends like this (the model is the one the installer pulled):

```text
    API keys     not found. Export a key, then: boss connect keys
    Codex        not found. Sign in with codex login --device-auth, then: boss connect codex
    Claude Code  not found. Sign in inside BOSS: boss connect claude
    Grok Build   not found. Sign in inside BOSS: boss connect grok
    Ollama       connected: qwen2.5-coder:7b
    Default      Ollama qwen2.5-coder:7b for every role (no hosted model was connected)
    Chat         the local Ollama model (OPENAI_* in .env point at it)
```

At least one row must say `connected`. To add an API key or a subscription you already have, follow the command on its row. [Model connections](MODEL_CONNECTIONS.md) covers each one.

**Check that a model actually answers:**

This optional check asks the model a question and uses model credit or local compute. `boss doctor` without `--probe` checks configuration without asking for a completion.

```bash
boss doctor --probe
```

```text
  ok    models connected: native
  ok    chat: a model answered
```

`chat: a model answered` means BOSS called the model and got a reply.

### Which local model

Use `qwen2.5-coder:7b`, the installer's default, or larger. Asked for a small command-line tip calculator, the 7b model's panel wrote a PRD about that tool: inputs, validation, the calculation, and the output. The `qwen2.5-coder:1.5b` model's PRD described a production web service with endpoints and deployment, and kept only the tip calculation from the request. A local model is slow without a GPU. On 8 CPUs, the 7b panel took about 24 minutes and the 1.5b panel about 6. To use a hosted model instead, see [Model connections](MODEL_CONNECTIONS.md).

To move from a smaller local model to the 7b model on Linux:

```bash
export OLLAMA_HOST=172.17.0.1:11434   # the address BOSS bound Ollama to; see BOSS_OLLAMA_URL in ~/boss/.env
ollama pull qwen2.5-coder:7b
boss connect ollama --local-model qwen2.5-coder:7b --yes
```

```text
  Ollama connected: qwen2.5-coder:7b at http://host.docker.internal:11434/v1 (unverified until a governed job uses it; no model was called)
  The 4 role(s) BOSS had given the previous local model now use Ollama qwen2.5-coder:7b
  Chat and the other OPENAI_* surfaces now use qwen2.5-coder:7b too (no hosted model is connected)
```

The jobs BOSS set (**Build**, **Review**, **Hard problems**, and **Routine work**) move to the new model with chat. A job you chose yourself in **Settings → Repo and models → Who does what** stays as you set it; the second line then ends with `1 role(s) you chose were left alone`. See [Model connections](MODEL_CONNECTIONS.md#ollama).

### If something goes wrong

- **`✗ api and worker were not restarted. The change is saved in .env; apply it with: boss update`.** The new setting is saved, but the running containers have not picked it up. Run the command it prints.
- **`chat` fails in `boss doctor --probe`.** Run `boss connect --summary` and follow the command on the row that is not connected.

## 4. Connect a GitHub repository

BOSS writes code on a branch named `boss/code/...` and opens a pull request. It never pushes to your default branch. It needs a repository that already exists and GitHub access that can push branches and open pull requests in it.

1. On GitHub, create a repository for BOSS to build in. Tick **Add a README file** so the repository has a default branch.
2. In Mission Control, open **Settings → Repo and models**. Until setup is finished it is a three-step page: **1 Repository**, **2 Model**, **3 Try it**.
3. In **GitHub access**, use the recommended **GitHub App**. Press **Create GitHub App**, finish GitHub's steps, install it on the account and repositories you want, then return to BOSS. On a fresh setup, **Other ways** contains personal access and the fine-grained token alternative.
4. In **Repository**, choose the connected App and pick a repository. For token access, use **Create token ↗**, grant **Contents** and **Pull requests** *Read and write* for only the repositories you select, then paste the token. Pick a repository and press **Connect**.
5. BOSS runs three checks against the repository — **Read**, **Write** (a blob nothing references, so no branch moves and no CI runs) and **Pull requests** (asking to open one for a branch that does not exist, so none is created) — and the row says **Working**.

**Check that it worked:** the Repository row reads **Working** with **Read ✓ Write ✓ Pull requests ✓**, and `boss doctor` reports the repository.

!!! note "Use the console, not `.env`"
    Setting `BOSS_GITHUB_OWNER` and `BOSS_GITHUB_REPO` in `.env` does not choose where builds go. Connect the repository on **Settings → Repo and models**. A repository an older install configured in `.env` shows there with the chip **From install**; **Replace token** moves it into BOSS.

### If something goes wrong

Without a connected repository, a build gets through the panel and the PRD, then stops at the build step with:

```text
No repository registered for project boss-dogfood. Connect a GitHub repository in Repo and models (/?area=setup&focus=repository), then rerun this step.
```

The **+ New project** form says the same before you start: with no repository connected, its **Which repository?** step reads "This project has no repository connected yet, and a build needs one" and links to the **Repository** card.

Connect the repository as above, then retry the step from the run (step 6).

## 5. Create your first project

A project in Mission Control is one piece of software you want built. Each one runs down a workflow: a panel writes a PRD, BOSS builds the code on a branch, runs your tests, and opens a pull request.

**Build** uses the default path, **Standard Product Delivery**. Its first step is research. Research is web research done by the Claude CLI on your Claude Code subscription, so it runs only when Claude Code is connected (step 3). Without it, for example with only a local Ollama model, the run records that step as **skipped** with the reason and goes straight on to the panel. You do not need to choose another workflow.

1. Open **Command Center**. The top of the page reads **Ask BOSS** and **What do you want to do?**
2. Type what you want built. Keep the first one small and testable, for example:

    ```text
    A tiny command-line tip calculator in Python: tip.py takes a bill amount and a tip
    percent and prints the tip and total, with a pytest test.
    ```

3. Press **Build →** (or Ctrl+Enter, ⌘+Enter on a Mac). The hint under the button reads "Down the governed path — a panel, a PRD you approve, then code". BOSS replies **Started.** and opens the run on the **Workflow** page.

The **+ New build** button opens a single form. Choose **New app**, enter a **Brief**, and add checks under **Done when**. Review the **Workflow** and **Repository**, then press **Start the project**. To extend an app BOSS has already built, choose **Add a feature**. If setup is incomplete, **Setup needed** lists the missing requirements; **Setup details** explains each fix and **Open setup** takes you to it. Starting spends model budget, and the form shows the approval steps before you start.

### From a terminal instead

The same start, through the API. Run it from the install directory; `boss-community.env.sh` loads the API address and project, and the next line uses the host tools' key (`boss` issues it; nothing is printed).

```bash
cd ~/boss
grep -q '^BOSS_HOST_API_KEY=.' .env || boss identity host-key
source ./boss-community.env.sh
export BOSS_API_KEY="$(sed -n 's/^BOSS_HOST_API_KEY=//p' .env)"
curl -fsS -H "Authorization: Bearer $BOSS_API_KEY" -H "Content-Type: application/json" \
  -d "{\"project_id\":\"$BOSS_PROJECT_ID\",\"actor\":\"ceo\",\"work_class\":\"project.new\",\"input\":{\"title\":\"Tip calculator\",\"directive\":\"A tiny command-line tip calculator in Python with a pytest test.\"}}" \
  "$BOSS_API_URL/v1/delivery-workflows/start"
```

This is the same call **Build** makes. The reply is the new run. It starts with `"status":"running"`, `"workflow_name":"Standard Product Delivery"`, and the `research` step in `current_frontier`; a few seconds later research is either running (Claude Code connected) or shows as skipped, and then `meeting` is running.

### If something goes wrong

- **`Could not start the build.`** Check `boss doctor` and step 3.

## 6. Follow the governed path

Open the run on the **Workflow** page. Runs are grouped as **Needs you**, **Running**, **Stopped**, and **Done**. On Standard Product Delivery, the steps run in this order:

1. **research.** With Claude Code connected, BOSS researches your request on the web as a governed job: each stage reserves budget, takes a model lease on the Claude Code subscription, and leaves a receipt, and the panel reads what it found. If the research fails partway, the step says so and the run carries on with whatever it had found. Without a usable Claude Code subscription, the step is shown as **skipped**. Hover the ⓘ for the reason, for example: `Skipped: research cannot run here. The panel works from your request instead. Research runs on the Claude Code subscription (the Claude CLI does the web search), and it cannot be used here: it is not connected.` The skip is also recorded as a `workflow.research_skipped` event. Nothing is researched and nothing is spent.
1. **meeting.** A panel of agents discusses the goal and drafts a PRD. On 8 CPUs with no GPU, this took about 24 minutes with the local 7b model.
1. **spec_review.** Review the drafted PRD in **Approvals** before the build proceeds. On an enforced install, automatic approval is off by default; a person with approval authority must approve it. If the Owner explicitly enables `BOSS_MEETING_PRD_AUTO_APPROVE=true`, the decision is instead marked **AUTO-APPROVED** and still appears in the audit trail. The PRD is titled with your request, for example `PRD: A tiny command-line tip calculator in Python...`. Read it here: it is what the build will be measured against.
1. **build.** BOSS clones your repository and runs the governed coding pipeline. You can follow it in **Factory**: Queued, Cloning repo, Designing plan, QA writing tests, Engineer implementing, Running tests, Adversarial review, Mutation check, Opening PR, Done. If the plan touches something sensitive, it stops at **Awaiting plan approval** and an approval appears in **Approvals**.
1. **preview** and **plrr** (production launch readiness). BOSS records the preview, then checks the built app against the PRD's operating requirements. Readiness can repair gaps and records what it changed in the run report. Standard Product Delivery has no second preview step. A preview record alone does not prove a live app is running; inspect its actual status and address.
1. **report.** A run report with the cost per step.
1. **deploy.** An approval gate. BOSS stops and asks before deploying anything. You do not need to deploy to see your software; stop at the pull request.
1. **monitor** and **notify**, after a deploy you approve.

Every step is recorded in **Activity log**, and a step that fails says why on the run.

Stopping a workflow also cancels its Discovery research. Once cancellation is
observed, Discovery starts no later research stage, and a late answer cannot
turn the cancelled run into a completed run. A request already submitted to a
model may still finish and report usage; cancellation does not promise zero
charge. If its receipt cannot be settled safely, BOSS records a **Reconcile
Discovery … stage …** decision and preserves the receipt and observed usage.
Its budget reservation may remain held, including when cancellation invalidated
the lease just before submission. Review that evidence before retrying; do not
blindly restart research or assume the held budget has been released.

!!! note "Verify this part on your own repository"
    For this guide, Build was run on a Community install with the local `qwen2.5-coder:1.5b` model: research was skipped, the meeting (about 7 minutes) and spec_review steps ran, and the build step was run up to the missing-repository stop shown in step 4. The pipeline stages after cloning, and the pull request, need a connected GitHub repository; the stage names above are the ones Factory shows.

### If something goes wrong

- **A step shows as failed.** The run shows the reason. Fix the cause, then press **Try ... again** on that step. BOSS queues the step again; the steps that already succeeded are not repeated.
- **The build stops with `Tests did not pass; not opening a PR.` or `Agent produced no file changes.`** The model could not finish the change. Use a larger model (step 3), make the request smaller, or say exactly which file to create and which test command to run.

## 7. Review the pull request and run the software

When the build step finishes, Factory shows the task as done with a link to the pull request, and a merge approval appears in **Approvals**: `Approve code change: <title> (PR #N)`. BOSS does not merge on its own.

Before you approve, review:

- **The PRD** from step 6. Does the change do what it says?
- **The pull request** on GitHub: the diff, and the tests BOSS wrote.
- **The evidence** on the Factory task: test results and the review outcome, for example `tests ✓ · review ✓`.
- **The receipt.** Inspect the pull-request receipt in **Activity log** and match it to this run. If the outcome is uncertain, resolve that state before retrying.

To run the software, check out the branch from the pull request on your own machine. Replace the repository URL and branch below with the values from your pull request, and choose a fresh local folder. For the tip calculator:

```bash
REPOSITORY_URL="https://github.com/YOUR-OWNER/YOUR-REPO.git"
PR_BRANCH="COPY-THE-BRANCH-NAME-FROM-YOUR-PR"
git clone "$REPOSITORY_URL" first-result-review
cd first-result-review
git checkout "$PR_BRANCH"
python3 tip.py 50 20
python3 -m pytest
```

Use the file names and commands from your pull request; the model chooses them. When you are satisfied, approve the merge in **Approvals**, or merge on GitHub.

!!! note "Verify this part on your own repository"
    This step needs a pull request on GitHub. It was not run for this guide: the install used to check this guide had no GitHub repository connected.

## Next steps

- Read [Use cases](use-cases.md) for what else a new install can do.
- Drive BOSS from a terminal with the [Metatron terminal](TERMINAL_API.md), or from your own agents with [MCP](MCP.md).
- Put Mission Control on a domain for your team with [Deploy](hostname.md).
- Report problems as described in [Feedback](FEEDBACK_LOOP.md).
