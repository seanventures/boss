# Use cases

These four scenarios show what a fresh BOSS Community install can do today. Each one follows the same pattern: BOSS prepares the work, records what it did, and stops for your approval before any gated effect.

!!! note "How BOSS runs"
    BOSS Community is a self-hosted control plane that you install on a machine you administer. The API and console bind to `127.0.0.1`, and the generated `.env` stays on that machine. Goals you submit never run unattended, and installing agent definitions or the five example workflows under `examples/` does not start any work. **Agent Studio** holds definitions you can test; it does not come with a ready-made workforce.

Throughout this page, four record types come up again and again:

| Record | When BOSS creates it |
| --- | --- |
| **Plan** | When BOSS turns a goal into steps you can review. |
| **Decision** | When a step is gated and needs your approval in **Approvals**. |
| **Budget reservation** | Before any step that would spend money runs. |
| **Receipt** | When a side effect actually happens, with a unique dedupe key. |

## Turn a goal into a plan you can review

**Goal:** Describe what you want in a sentence and read the resulting plan before BOSS takes any gated step.

**In the console:** Open **Command Center**. At the top, **Ask BOSS** prompts "What do you want to do?" and reminds you that gated actions require your approval. Type your goal, then choose a path:

- **Talk to BOSS** discusses the goal and starts nothing unless you say so.
- **Build** is the governed path: a panel, then a PRD you approve, then code.

Read the plan before you approve it. If no model is connected, model work is shown as unavailable while the rest of the control plane keeps running.

**What BOSS records:** The plan under review. Each gated step becomes a decision in **Approvals**, each spending step needs a budget reservation first, and each side effect gets a receipt when it happens, not when you type the sentence.

**What you approve:** Every gated step, any spend, and anything that would leave this machine.

!!! note
    A proposal is not finished work. After you approve it, Software Factory pages and APIs can carry a governed code task, but they are not an unattended pipeline, and a preview record does not mean a live app has opened.

## Connect a model you already have

**Goal:** Use an API key or coding subscription you already control, on this machine.

**In the console:** Open **Settings → Repo and models**, then **+ Add model** › **API key**, pick the provider and paste the key. BOSS asks the model one tiny question — budgeted and receipted — and keeps the key only if it answers, so **Working** means the model responded. Logins on your laptop are not copied to the VM. For supported providers and their limits, see [model connections](MODEL_CONNECTIONS.md).

**What BOSS records:** The connection, in the local secret store. Saving a key does not create a plan, open a decision, reserve budget, or write a cost receipt. Those records appear only when a governed model job actually runs.

**What you approve:** Any live check or job that would spend.

There is no hosted account to sign into and no marketplace of hosted connectors. Self-hosted SSO against an identity provider you run is configured separately from model connections.

## Check budget and receipts from the terminal

**Goal:** Confirm whether money was reserved and whether a side effect was recorded, straight from the ledger rather than from a model's reply.

**In the console:**

- **Govern** sets how hard BOSS is allowed to run.
- **Activity log** lists the events, steps, jobs, receipts, and deadletters that exist.
- **Evidence** summarizes receipts and cost when those records exist.

Exact dollar amounts require a billing-readable key. Without one, the cost label shows as empty, unknown, or unavailable. Treat a blank as unknown, not as zero.

**From the terminal:** Run `make metatron`, then use `/projects`, `/ops budget`, `/ops receipts`, `/describe`, and `/op` to make a single read. Copy the operation id from the live list that `/ops` prints. The terminal reads the same API records; it does not keep a separate ledger. See [Ask the terminal](walkthroughs.md#ask-the-terminal) for the full sequence.

**What BOSS records:** Nothing new. Status reads return the budget reservations and receipts the API already stored, without creating a plan or a decision.

**What you approve:** Any new reserve or consume, and any call the API holds for approval. A successful status call only means that read was accepted, not that later work finished.

## Hand an external action to a person

**Goal:** Prepare an email, a publish, or a ticket, and stop before it leaves an account you control.

**In the console:** Open **Approvals**, headed "Agents proposed — you decide." **Needs action** is the queue of pending and delegated cards, and each card shows the question before you approve or reject it. **Communications** is the outbox, where a draft can wait while the send is held for approval.

The five [example workflows](https://github.com/seanventures/boss/blob/main/examples/README.md) follow this shape: a video publish, meeting notes that become tickets, a newsletter send, a sales draft that stops at a person, and a content review. Their setup scripts only create definitions in your project; they do not start a run.

**What BOSS records:** A plan of the prepared steps, then a decision for the step that would leave your accounts. Spending on that step needs a budget reservation. When an approved side effect happens, BOSS writes a receipt with a dedupe key so the same effect is never recorded twice. While the decision is pending, no send receipt exists.

**What you approve:** The send, publish, or ticket write, which you approve yourself in **Approvals**. BOSS does not take that step while the card is waiting.

## Agents and their activity

**Agent Studio → Team** shows planned roles and observed handoffs. On a new install the planned layout is empty, and moving a role there does not route tasks. **Agent Studio → Roster** lists every agent and its recent work; agents are not running on their own after install.

## Report a vulnerability

Report suspected vulnerabilities privately to [boss@sean.net](mailto:boss@sean.net) rather than opening a public issue. See [SECURITY.md](https://github.com/seanventures/boss/blob/main/SECURITY.md).
