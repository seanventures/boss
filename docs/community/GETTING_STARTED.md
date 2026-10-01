# Getting started

This guide takes you from a fresh machine to software that BOSS built for you. You will:

1. Install BOSS.
2. Sign in to Mission Control.
3. Connect a model.
4. Connect a GitHub repository.
5. Create your first project.
6. Follow it down the governed path: panel, PRD, approval, code, tests.
7. Review the pull request and run the software.

Plan on an hour or more. The install takes several minutes. On a machine without a GPU, a local model needs tens of minutes for the panel alone.

The command output and messages quoted below were recorded on a fresh Ubuntu 24.04 install (8 CPUs, 16 GB of RAM, no GPU) with a local Ollama model and no paid API key. Steps 4, 6, and 7 depend on GitHub. The notes in those steps say which parts you will see for the first time on your own repository.

## What you need

- An Ubuntu 22.04 or 24.04, or Debian 12 or 13, machine with `curl` and `sudo`. macOS with Docker Desktop also works; see the [Quickstart](QUICKSTART.md#before-you-begin) for the full list.
- About 100 GB of free disk. The installer warns below 80 GB.
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
Sign in with the password from: boss password
```

Then show the console password:

```bash
boss password
```

It prints one line: the password. Copy it. The password is separate from the API key and is never written to a log.

### Open the console

**On the machine where BOSS is installed:** open `http://127.0.0.1:3000`.

**From another computer:** BOSS listens on `127.0.0.1` only, so forward both ports over SSH and leave the command running:

```bash
ssh -N -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-server
```

Then open `http://127.0.0.1:3000` on the computer that ran `ssh`. If ports 3000 or 8000 are taken on that computer, forward other local ports, for example `-L 3300:127.0.0.1:3000`, and open that port instead.

**For your team, on a domain or private IP:** see [Deploy](hostname.md).

The sign-in page shows **BOSS Mission Control**, a **Console password** field, and a **Sign in** button. Paste the password and sign in. Mission Control opens on **Command Center**. More about sessions is in [Sign in to Mission Control](CONSOLE_AUTH.md).

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
  No hosted model is connected; every role already has a model you chose, so none was changed
  Chat and the other OPENAI_* surfaces now use qwen2.5-coder:7b too (no hosted model is connected)
```

As the second line says, the build roles keep their old model. Change them in Mission Control: **Settings → Repo and model → Who does what**. Set the model for the **Implementer**, the **Reviewer**, **Hard problems**, and **Routine work** to `qwen2.5-coder:7b`. Each setting is saved for the project and applies on the next run.

### If something goes wrong

- **`✗ api and worker were not restarted. The change is saved in .env; apply it with: boss update`.** The new setting is saved, but the running containers have not picked it up. Run the command it prints.
- **`chat` fails in `boss doctor --probe`.** Run `boss connect --summary` and follow the command on the row that is not connected.

## 4. Connect a GitHub repository

BOSS writes code on a branch named `boss/code/...` and opens a pull request. It never pushes to your default branch. It needs a repository that already exists and a token that can push branches and open pull requests in it.

1. On GitHub, create a repository for BOSS to build in. Tick **Add a README file** so the repository has a default branch.
2. Create a fine-grained personal access token at `https://github.com/settings/personal-access-tokens/new`:
    - **Repository access**: *Only select repositories*, then pick the one repository.
    - **Permissions → Repository permissions**: set **Contents** and **Pull requests** to *Read and write*. Leave everything else at no access.
    - Copy the token. GitHub shows it once.
3. In Mission Control, open **Settings → Repo and model**. The page is headed **Connect a repo and model**.
4. In the **Repository** card, paste the token into **Token**, enter `owner/name` in **Repository**, and press **Connect and test**. BOSS checks that it can push before it saves anything. **How do I get a token?** on the same card repeats the token steps.

**Check that it worked:** the **Connect a repository** step on that page is marked done, and `boss doctor` no longer warns about the repository.

!!! note "Verify this part on your own repository"
    The install used to check this guide had no GitHub repository, so this step was not run for it. The labels above are the ones on the **Repo and model** page.

!!! note "Use the console, not `.env`"
    Setting `BOSS_GITHUB_OWNER` and `BOSS_GITHUB_REPO` in `.env` does not choose where builds go. Connect the repository on **Settings → Repo and model**.

### If something goes wrong

Without a connected repository, a build gets through the panel and the PRD, then stops at the build step with:

```text
No repository registered for project boss-dogfood and no legacy default configured. Register a GitHub connection and a repository binding first.
```

Connect the repository as above, then retry the step from the run (step 6).

## 5. Create your first project

A project in Mission Control is one piece of software you want built. Each one runs down a workflow: a panel writes a PRD, BOSS builds the code on a branch, runs your tests, and opens a pull request.

!!! warning "Choose a path without a research step"
    In this release, the default path, **Standard Product Delivery**, starts with a research step that BOSS Community does not run. A run on that path stops at once with `Discovery research is unavailable in Community until model calls have durable leases, budget reservations, and reconciled receipts.` Pick **Backend Service Setup** instead, as below. It runs a panel, a spec review, and the build.

1. Open **Command Center**. The top of the page reads **Ask BOSS** and **What do you want to do?**
2. In the **Workflow** dropdown under the input, choose **Backend Service Setup**. BOSS saves it as this project's default path and shows **Saved**. You only need to do this once.
3. Type what you want built. Keep the first one small and testable, for example:

    ```text
    A tiny command-line tip calculator in Python: tip.py takes a bill amount and a tip
    percent and prints the tip and total, with a pytest test.
    ```

4. Press **Build →** (or Ctrl+Enter, ⌘+Enter on a Mac). The hint under the button reads "Down the governed path — a panel, a PRD you approve, then code". BOSS replies **Started.** and opens the run on the **Workflow** page.

The **+ New project** button in the top bar does the same thing as a four-step form: **What you are building**, **How it gets built**, **Where it lives**, and **Review**. In **How it gets built**, set **Which path should it take?** to **Backend Service Setup**. In **Where it lives**, pick the repository you connected in step 4. Then press **Start the project**.

### From a terminal instead

The same start, through the API. Run it from the install directory; `boss-community.env.sh` loads the API address, key, and project without printing the key.

```bash
cd ~/boss
source ./boss-community.env.sh
curl -fsS -H "Authorization: Bearer $BOSS_API_KEY" -H "Content-Type: application/json" \
  -d "{\"project_id\":\"$BOSS_PROJECT_ID\",\"workflow_key\":\"backend_service_setup\",\"input\":{\"title\":\"Tip calculator\",\"directive\":\"A tiny command-line tip calculator in Python with a pytest test.\"}}" \
  "$BOSS_API_URL/v1/delivery-workflows/start"
```

The reply is the new run. It starts with `"status":"running"`, `"workflow_name":"Backend Service Setup"`, and the `meeting` step first in `current_frontier`.

### If something goes wrong

- **The run stops at `research` with `Discovery research is unavailable in Community...`.** The run used Standard Product Delivery. Set the **Workflow** dropdown to **Backend Service Setup** and press **Build →** again.
- **`Could not start the build.`** Check `boss doctor` and step 3.

## 6. Follow the governed path

Open the run on the **Workflow** page. Runs are grouped as **Needs you**, **Running**, **Stopped**, and **Done**. On Backend Service Setup, the steps run in this order:

1. **meeting.** A panel of agents discusses the goal and drafts a PRD. On 8 CPUs with no GPU, this took about 24 minutes with the local 7b model.
2. **spec_review.** The PRD is approved. In this release, PRD approval is automatic by default. **Approvals** shows the decision "Approve publishing the PRD the team drafted" marked **AUTO-APPROVED**, with the PRD attached. Read it here: it is what the build will be measured against.
3. **build.** BOSS clones your repository and runs the governed coding pipeline. You can follow it in **Factory**: Queued, Cloning repo, Designing plan, QA writing tests, Engineer implementing, Running tests, Adversarial review, Mutation check, Opening PR, Done. If the plan touches something sensitive, it stops at **Awaiting plan approval** and an approval appears in **Approvals**.
4. **report.** A run report with the cost per step.
5. **deploy.** An approval gate. BOSS stops and asks before deploying anything. You do not need to deploy to see your software; stop at the pull request.

Every step is recorded in **Activity log**, and a step that fails says why on the run.

!!! note "Verify this part on your own repository"
    For this guide, the meeting and spec_review steps were run and the build step was run up to the missing-repository stop shown in step 4. The pipeline stages after cloning, and the pull request, need a connected GitHub repository; the stage names above are the ones Factory shows.

### If something goes wrong

- **A step shows as failed.** The run shows the reason. Fix the cause, then press **Try ... again** on that step. BOSS queues the step again; the steps that already succeeded are not repeated.
- **The build stops with `Tests did not pass; not opening a PR.` or `Agent produced no file changes.`** The model could not finish the change. Use a larger model (step 3), make the request smaller, or say exactly which file to create and which test command to run.

## 7. Review the pull request and run the software

When the build step finishes, Factory shows the task as done with a link to the pull request, and a merge approval appears in **Approvals**: "Approve code change: <title> (PR #N)". BOSS does not merge on its own.

Before you approve, review:

- **The PRD** from step 6. Does the change do what it says?
- **The pull request** on GitHub: the diff, and the tests BOSS wrote.
- **The evidence** on the Factory task: test results and the review outcome, for example `tests ✓ · review ✓`.
- **The receipt.** Opening the pull request is recorded as a receipt in **Activity log**, so it is never repeated by a retry.

To run the software, check out the branch from the pull request on your own machine. For the tip calculator:

```bash
git clone https://github.com/<owner>/<repo>.git
cd <repo>
git checkout boss/code/<run-id>      # the branch named on the pull request
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
