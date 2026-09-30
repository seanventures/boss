# Use cases

BOSS Community is a self-hosted control plane. You install it on a machine you
administer. The API and the console bind to `127.0.0.1` on that machine. The
generated `.env` stays there and is not uploaded.

These are four things a new install can already do. Each one stops for you
before a gated effect. BOSS is not a public hosted service, and a goal you
submit does not run unattended.

Installing agent definitions does not start work. Installing the five example
workflows under `examples/` does not start work either. Agent Studio holds
definitions you can test. It does not ship a workforce.

## Submit a goal and review the plan

**Goal.** Turn a sentence into a plan you can read before BOSS takes a gated
step.

**In the console.** Open **Command Center**. At the top, **Ask BOSS** asks
"What do you want to do?" and says that gated actions require your approval.
Type the goal. **Talk to BOSS** can discuss it and starts nothing unless you
say so. **Build** is the governed path: a panel, a PRD you approve, then code.
Read that plan before you approve it. If no model is connected, model work
stays unavailable even though the control plane is up.

**What BOSS records.** The plan you are reviewing. A gated step becomes a
**decision** in **Approvals**. A step that would spend needs a **budget**
reservation before it runs. A side effect gets a **receipt** when it happens,
not when you type the sentence.

**What you still approve.** The plan's gated steps, any spend, and anything
that would leave this machine. A proposal is not finished work. Software
Factory pages and APIs can carry a governed code task after you approve it.
They are not an unattended pipeline, and a preview record does not mean a live
app opened.

## Connect a model you already have

**Goal.** Use an API key or a coding subscription you already control, on this
machine.

**In the console.** Open **Repo and model** under Settings. Add the provider
and key you already have. Saving stores the key on this machine and marks the
model **unverified**. Saving does not call the provider, and a green setup
step is not a model response. A laptop login is not copied onto the VM.
Supported paths and their limits are in [model connections](MODEL_CONNECTIONS.md).

**What BOSS records.** The connection in the local secret store. It does not
record a **plan**. It does not open a **decision**, reserve **budget**, or
write a cost **receipt** just because the key was saved. Those appear only
when a later governed model job actually runs.

**What you still approve.** Any live check or job that would spend. There is
no hosted Account Connect account to sign into, and no marketplace of hosted
connectors. Self-hosted SSO against an identity provider you run is separate
from this model connection.

## Ask the terminal for budget and receipt status

**Goal.** See whether money was reserved and whether a side effect was written
down, without guessing from a model's reply.

**In the console.** **Govern** is where you set how hard BOSS is allowed to
run. **Activity log** lists events, steps, jobs, receipts, and deadletters
that exist. **Evidence** summarizes receipts and cost when those records
exist. Exact dollar amounts need a billing-readable key. Otherwise the label
stays empty, unknown, or unavailable. Do not treat a blank as zero you
invented.

**What BOSS records.** The **budget** reservations and **receipts** the API
already stored. A status read does not create a **plan** or a **decision**.
The [terminal](walkthroughs.md#ask-the-terminal) is how you ask: `make metatron`,
then `/projects`, `/ops budget`, `/ops receipts`, `/describe`, and `/op` for
one read. Copy the operation id from that live list. The terminal does not
keep a second ledger.

**What you still approve.** A new reserve, consume, or any call the API holds.
A successful status call only means that read was accepted. It does not mean
later work finished.

## Hand an external action to a person

**Goal.** Prepare an email, a publish, or a ticket, and stop before it leaves
an account you control.

**In the console.** Open **Approvals**. The heading is "Agents proposed — you
decide." **Needs action** is the queue of pending and delegated cards. A card
shows the question before you approve or reject it. **Communications** is the
outbox: a draft can sit there, and sending still waits. The five workflows in
[examples](https://github.com/seanventures/boss/blob/main/examples/README.md) follow this shape, including a video
publish, meeting notes that become tickets, a newsletter send, a sales draft
that stops at a person, and a content review. Running a setup script only
creates definitions in your project. It does not start a run.

**What BOSS records.** A **plan** of the prepared steps, then a **decision**
for the step that would leave your accounts. Spend on that step needs a
**budget** reservation. A **receipt** with a dedupe key is written when an
approved side effect happens, so the same effect is not recorded twice. While
the decision is waiting, there is no send receipt.

**What you still approve.** The send, the publish, or the ticket write. You
do it yourself in **Approvals**. BOSS does not take that step while the card
is waiting.

## Boundaries

**Team structure** shows planned roles and observed handoffs. On a new install
the planned layout starts empty. Moving a role does not route tasks. **Agent
work** lists recent activity by role. It is not a workforce that woke up at
install.

Install does not need Plane, an ops token, or a private registry.

Report a suspected vulnerability to [boss@sean.net](mailto:boss@sean.net). Do
not open a public issue. See [SECURITY.md](https://github.com/seanventures/boss/blob/main/SECURITY.md).
