# Walkthroughs

Three walks for someone installing BOSS on a machine they administer. These
pages use commands and name the console areas. They do not include pictures.
A later install can add pictures; do not treat a missing picture as a missing
step.

The matching tasks are in [use cases](use-cases.md). Install details that this
page does not repeat live in the [quickstart](QUICKSTART.md) and the
[installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md).

## Install from a checkout

The GitHub one-liner installs release `v1.0-rc1`, commit
`7b88a18a2b4cd0b14b09a47fc415f7ffd4580066`. Do not point the download at
another host.

From a checkout you already have:

```sh
./install.sh --dir "$PWD"
```

Run it on the machine you are installing onto. It does not SSH to a maintainer
machine. It does not need Plane, an ops token, or a private registry. It binds
the API and the console to `127.0.0.1`. It writes `.env` on that machine and
does not upload it. Keep that file private and do not commit it.

The installer asks for a model key you already have, or you can press Enter
and connect one later. Do not put a key on the command line. The host it
expects is a supported Ubuntu or Debian machine with sudo, as the installer
guide describes.

When the installer finishes, open [http://127.0.0.1:3000](http://127.0.0.1:3000)
on that same machine. You should get the console sign-in, not a hosted
account page. There is no hosted Account Connect signup.

Use an SSH tunnel only when BOSS is running on a VM you administer and you are
sitting at another computer:

```sh
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-vm
```

`user@your-vm` means your account on that VM. It does not mean a maintainer
machine. Leave the tunnel open and open
[http://127.0.0.1:3000](http://127.0.0.1:3000) on the computer that started
it. The ports on the VM stay bound to loopback.

The console password is not the API key. The command that shows it is
documented in [scripts/install/README.md](https://github.com/seanventures/boss/blob/main/scripts/install/README.md).
From the install directory:

```sh
python3 scripts/install/console_password.py show
```

Run it in a terminal so the password is not piped into a log. Do not paste
the password or the API key into a ticket.

The optional Goldeneye kit is software you can run on this same machine. It
is not access to someone else's server. You do not need it to sign in.

Self-hosted SSO against an identity provider you run is a later setup. The
first sign-in is the owner password created on this machine.

## First goal in Command Center

Sign in at [http://127.0.0.1:3000](http://127.0.0.1:3000).

Open **Command Center**. The top of the page is **Ask BOSS**. The title is
"What do you want to do?" Under it: "Gated actions require your approval."
The box says "Describe what to build or ask a question…"

Type one concrete goal. **Talk to BOSS** answers and starts nothing unless
you say so. **Build** follows the governed path and still stops for a PRD
you approve before code. If you have not connected a model, model work stays
visibly unavailable. The control plane can still be up.

Read the reply as a plan, not as finished work. Software Factory pages and
APIs may be where a governed code task shows up after you accept that path.
They do not run unattended. A preview or sandbox record does not mean a live
app opened.

Then open **Approvals**. You should see the decision queue headed "Agents
proposed — you decide." **Needs action** lists pending and delegated cards.
Each card is the question, not a completed effect. Approve or reject it
yourself. If nothing is waiting, the queue says "Queue is clear" and
"Nothing needs your signature." Lower on the same area, approval rules are
headed "What requires your approval — and why."

Then open **Activity log**. The heading is "Activity log" and the line under
it is "Central log of events, steps, jobs, receipts, and deadletters." You
should see only rows BOSS recorded for this project. An empty log means those
records are not there yet. If the log cannot be loaded, it says it is
unavailable and that no rows were invented. A line here is not proof that
later work finished.

**Agent work** lists recent activity by role. On a first goal, that list may
still be empty. **Team structure** is the planned role layout plus observed
handoffs. Changing the plan does not route tasks. **Agent Studio** is where
definitions live so you can test one. Installing a starter template does not
start work, and it does not give you a workforce. Installing the five example
workflows under `examples/` also starts nothing. A setup script only writes
definitions you can change or delete.

## Ask the terminal

Two entries open the same Metatron terminal. Neither prints the API key.

From the install directory, the community terminal entry is:

```sh
make metatron
```

That opens the terminal inside the API container and uses the install's
configured key and project.

From a checkout where you have already set `BOSS_API_URL`, `BOSS_API_KEY`,
and `BOSS_PROJECT_ID` in the environment:

```sh
python3 scripts/metatron_tui.py
```

The prompt is `metatron>`. The command reference is
[terminal and API access](TERMINAL_API.md). How this terminal sits next to
the HTTP API and MCP is [control surfaces](CONTROL_SURFACES.md).

Use these commands in order:

1. `/projects` lists projects returned by the API on this machine. A normal
   install seeds `boss-dogfood`. That id is your local project, not a shared
   server. `/project` plus the id selects it.
2. `/ops` lists product domains on the running API. `/ops budget` searches
   budget reads and writes. `/ops receipts` searches receipt reads. `/ops *
   --page 1` browses one page if you are not searching by name.
3. `/describe` plus an operation id from that list shows the parameters and
   body. Copy the id the terminal printed. Do not invent one.
4. `/op` plus that same id sends one request. Add JSON only when `/describe`
   says the call needs `path`, `query`, or `body`.

A successful `/op` means the API accepted that one call. It is not proof that
a later job, model turn, deploy, or external send finished. The API can still
deny or hold a call for permission, budget, or approval. After a write, look
again at **Approvals**, **Activity log**, and a budget or receipt read before
you treat the work as done.

## Report a vulnerability

Report suspected vulnerabilities privately to
[boss@sean.net](mailto:boss@sean.net). Do not open a public issue, and do not
include credentials in the report. See [SECURITY.md](https://github.com/seanventures/boss/blob/main/SECURITY.md).
