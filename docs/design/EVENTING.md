# BOSS Eventing: agents that wake up on their own

**What this is:** how BOSS agents get woken by things happening in other systems, explained for people. Examples: a Slack message, a Teams chat, a new ServiceNow incident, a GitHub issue.
**Status:** for your review. Nothing in it is built yet.
**Date:** 14 November 2026
**Engineering detail:** [`EVENTING_LLD.md`](EVENTING_LLD.md), in the same folder. That is the low-level design for the build agents, with code references, tests and research sources.
**Builds on:** [`AGENT_HOSTS.md`](AGENT_HOSTS.md) (agents running in microVMs).

---

## How this document is organised

1. **The problem.** What we want, and what BOSS already has.
2. **The big picture.** One diagram.
3. **The components.** Each box in the diagram, one at a time.
4. **The flows.** Four step-by-step walkthroughs:
   - Flow 1: someone talks to an agent in Slack.
   - Flow 2: a ServiceNow incident wakes an agent with no person involved.
   - Flow 3: the event reaches an agent running in a microVM.
   - Flow 4: things go wrong.
5. **Who the agent acts for.** Identity.
6. **Safety guards.** Loops, storms and spend.
7. **How others do it.**
8. **Why we chose this design.**
9. **What changes from today.**
10. **The build plan.**
11. **Decisions we need from you.**
12. **Glossary.**

---

## 1. The problem

### 1.1 What we want

You want agents that **work on their own**, not only when someone clicks a button:
- An agent should **wake up when something happens** in another system: a message in Slack or Teams, a new incident in ServiceNow, a changed record in Salesforce, a GitHub issue, an email.
- People should **still be able to call it** directly, from the console, the API, another agent, or by talking to it in Slack or Teams.
- **SaaS apps and messaging come first.** Stream sources (Kafka, AWS SQS, Google Pub/Sub, Azure Service Bus) come later, when a customer needs them. The design leaves a clean slot for them.
- **BOSS stores events itself, in Postgres.** We do not run Kafka as BOSS's backbone.

### 1.2 What BOSS already has

BOSS is not starting from zero:

| Already there | What it does today |
|---|---|
| **Agent triggers** | Four kinds: schedule, internal BOSS event, signed webhook, and a polled "monitor" check. Each one starts a run. |
| **Slack and Telegram, two-way** | Only for approvals: people press Approve or Reject in Slack. BOSS already checks Slack's signatures, links a Slack user to a BOSS person, and sends messages through an outbox. |
| **Jira and Plane connectors** | Webhooks and polling for tickets. |
| **GitHub webhooks** | Used by the build workflows. |

### 1.3 What is missing

- **People can't talk to an agent in Slack.** A mention of the bot is read and then thrown away.
- **No Teams at all.**
- **No ServiceNow, Salesforce or email events.**
- **No single, reliable intake.** Today each source is wired separately. Internal events can be lost on an error, there's no protection against duplicates or event storms, and failed events can't be replayed.
- **Slow pickup.** A local agent can wait up to 15 seconds before it notices new work.
- **One conversation per trigger.** A second Slack message sent while the agent is busy is skipped instead of queued.

---

## 2. The big picture

```
   THE OUTSIDE WORLD                      BOSS CONTROL PLANE                          WHERE AGENTS RUN
   ─────────────────                      ──────────────────                          ────────────────
   Slack ──────────┐
   Microsoft Teams ┤                ┌───────────────────────────┐
   ServiceNow ─────┤   ① push       │ 1. FRONT DOOR             │
   GitHub ─────────┼──────────────► │    check signature        │
   Jira / Plane ───┤   (webhook)    │    save event, say "got it"│
   any app with ───┘                └─────────────┬─────────────┘
   a signed webhook                               │
                                                  ▼
   ServiceNow, Gmail, ◄── ② pull ── ┌───────────────────────────┐
   Outlook (some        (polling or │ 2. EVENT INBOX (Postgres) │  every event saved once,
   sources are polled)  subscribe)  │    duplicates dropped     │  never lost
                                    └─────────────┬─────────────┘
   (Kafka, SQS, Pub/Sub:                          ▼
    later, same slot)               ┌───────────────────────────┐
                                    │ 3. ROUTER                 │  "which agent cares?"
                                    │    rules + safety guards  │  "who is it for?"
                                    └─────────────┬─────────────┘
                                                  ▼
                                    ┌───────────────────────────┐
                                    │ 4. WAITING RUN            │  a run in the agent's
                                    │    (Postgres)             │  queue, ready to claim
                                    └─────────────┬─────────────┘
                                                  │  agents PULL work; nothing is pushed
                             ┌────────────────────┴────────────────────┐
                             ▼                                         ▼
                   LOCAL AGENT                                 MICROVM AGENT (replica)
                   (BOSS worker)                               via vsock → boss-hostd
                   "any work for me?"                          → run plane: "any work for me?"
                             │                                         │
                             └───────────────────┬─────────────────────┘
                                                 ▼
                                    ┌───────────────────────────┐
                                    │ 5. AGENT WORKS            │ models, tools, MCP
                                    └─────────────┬─────────────┘ (through the gateways)
                                                  ▼
                                    ┌───────────────────────────┐
                                    │ 6. REPLY OUTBOX           │ ──► answer in the Slack
                                    │    (bot tokens stay here) │     thread / Teams chat /
                                    └───────────────────────────┘     ServiceNow work note
```

**The picture in five sentences:**
1. Events come in through one front door. It checks they are genuine, saves them and answers "got it" straight away.
2. Every event is saved once in Postgres, so nothing is lost and nothing runs twice.
3. A router decides which agent should handle the event and who the agent is acting for. Safety guards stop loops, storms and runaway spend.
4. The event becomes a **waiting run**. Agents, whether local or in microVMs, **pull** work when they have room; nothing is pushed into them.
5. The agent's answer goes out through a reply outbox, so the agent never holds the Slack or Teams bot token.

---

## 3. The components

Each component gets three short answers: **what it is**, **what it does** and **why we need it**.

### 3.1 Source adapters

**What they are.** One small plug-in per outside system: Slack, Teams, ServiceNow, Salesforce, GitHub, Jira/Plane, Gmail, Outlook, and a generic webhook. They follow the same pattern as the Jira and Plane ticket connectors you already have.

**What each adapter does.** It answers the same questions for its system:
- **Is this genuine?** It checks the system's signature, so nobody can fake a Slack message to BOSS.
- **What happened?** It turns the system's own format into one standard BOSS event shape: who, what, where, when.
- **Is it one of ours?** It spots messages the agent itself wrote, so the agent doesn't answer itself forever.
- **How do we reply?** It knows how to post into a Slack thread, a Teams chat or a ServiceNow work note.
- **Push or pull?** Some systems call BOSS (push). For others BOSS asks regularly (pull), or holds a subscription open.

**Why we need them.** A new system is just a new adapter. Nothing else changes. Kafka or SQS will later be just another adapter of the "subscribe" kind.

### 3.2 The front door (intake)

**What it is.** One public web address per connection, served by the BOSS API you already run.

**What it does. Four things, and nothing more:**
1. Checks the signature.
2. Saves the raw event in the inbox.
3. Answers "got it" fast. Slack gives us 3 seconds; our target is under 0.3 seconds.
4. Wakes the router.

**Why so little.** If the front door did real work, a slow model call could miss Slack's 3-second window. Slack would then retry, and could even switch off our app. Saving first and working afterwards is the pattern every serious integration uses.

### 3.3 The event inbox

**What it is.** A table in Postgres holding every event that arrived.

**What it does:**
- **Never loses an event.** It is saved before we say "got it".
- **Never handles one twice.** Systems often send the same event again (Slack retries, GitHub re-delivers). We use each system's own event ID, and a second copy is simply ignored.
- **Keeps a status per event:** routed, ignored, filtered, refused, failed, dead.
- **Dead letters and replay.** An event that keeps failing goes to a "dead letter" list. Someone can look at it and replay it once the problem is fixed.
- **Retention.** The raw content is kept 14 days, and the summary 30 to 90 days (a decision for you).

**Why we need it.** It is the record of everything that happened: what woke which agent, when, and why.

### 3.4 The router

**What it is.** Rules that decide which agent handles which event.

**What a rule says, in plain words:**
- *"Messages that mention @BOSS in #support go to the Support agent."*
- *"New ServiceNow incidents with priority 1 or 2 go to the Incident agent."*
- *"GitHub issues labelled `bug` in repo X go to the Triage agent."*

**What it does:**
- **Matches each event against the rules**, in order. It uses a simple filter language (field equals, contains, is one of, changed, and so on).
- **Uses an optional cheap-model "triage"** for messy inputs, such as a busy support channel or an email inbox, where simple rules aren't enough. It is off by default and has hard daily cost caps.
- **Picks the conversation.** A Slack thread, a Teams chat or a ServiceNow record each maps to one BOSS conversation. Follow-up messages continue it, and messages are queued while the agent is busy, never skipped.
- **Decides who the agent is acting for** (section 5).
- **Applies the safety guards** (section 6).

### 3.5 Waiting runs and the pull

**What it is.** The router doesn't call the agent. It creates a **waiting run** in Postgres, in the agent's own queue.

**What it does.** Every running copy of an agent holds a request open that asks "any work for me?". This is called a long-poll. The moment a run is saved, Postgres sends a wake-up signal, and the first copy with a free slot claims the run.
- A local agent asks the BOSS API directly.
- A microVM agent asks through vsock → `boss-hostd` → the run-plane API, which is the same path as everything else a VM does.

**Who asks: the host, not each VM.** Each host's `boss-hostd` keeps **one** request open to BOSS for all its VMs. The request is **empty**: just "any work for Host B?". BOSS already knows which VMs run on that host, how many slots each has, and how many are busy (it handed out the runs and sees them finish). The host reports only changes, such as a VM dying or draining. When work arrives, BOSS picks the VM, records that the run belongs to that VM (the lease), and answers the host. The host hands the run to that VM over vsock in well under a millisecond. VMs never contact BOSS to ask for work.

```
 event saved ──► BOSS wakes Host B's waiting request
                   "run 77 → VM 9 (Incident agent), lease epoch 1"
 Host B ──vsock──► VM 9 starts run 77
 Host B immediately sends its next request (one open request per host, always)
```

- **How BOSS picks the VM:** only VMs of the right agent, on that host, that are healthy and have a free slot. Among those, the one with the most free slots.
- **If the VM died a moment ago:** the host can't deliver the run, so it hands the run back at once and another VM picks it up within 3 seconds.
- **Traffic:** one open request per host. 20 hosts means 20 requests in total, whether there are 50 VMs or 7,000. No load on the database while idle.

**Why pull instead of push.** Section 8 explains in full. In short:
- nothing is lost if a VM dies, because another one claims the run;
- busy agents don't get overloaded;
- the VM needs no new way in;
- one mechanism serves every way of starting an agent.

### 3.6 The reply outbox

**What it is.** BOSS's existing chat outbox, extended.

**What it does:**
- **Holds the agent's replies until they are sent.** The agent calls a `chat.reply` tool, and the reply is saved in the outbox and then sent to Slack or Teams.
- **Respects each system's speed limits.** Slack allows about 1 message per second per channel, and Teams about 7 per second per conversation. If a system says "slow down", the outbox waits and retries.
- **Shows progress.** The agent can post a "Working on it…" status and then edit it in place into the final answer, so people see it is alive.
- **Never sends twice.** The same reply is never posted twice, even if the agent's VM fails over halfway.

**Why we need it.** The bot tokens (the Slack and Teams passwords) stay in BOSS's vault and are used only by the outbox. They never enter an agent or a VM.

### 3.7 Pollers and subscriptions

Some systems can't, or don't by default, call BOSS:
- **ServiceNow** can push with a small script, but we also poll its records every few minutes as a safety net.
- **Gmail** pushes through Google Pub/Sub, and the "watch" must be renewed every day.
- **Outlook** pushes through Microsoft Graph subscriptions, which expire within days and must be renewed.
- **Salesforce** has a streaming subscription (its Pub/Sub API).

**What runs them.** Small background jobs in the BOSS API. Each one is protected by a database lease, so exactly one copy runs even with two API instances. Each remembers where it stopped (a cursor). After any gap, it catches up from there and nothing is missed.

---

## 4. The flows

### Flow 1: someone talks to an agent in Slack

Priya, an engineer, writes in `#support`: *"@BOSS why did last night's payments job fail?"*

```
 ① Slack ──► BOSS front door: check signature ✓, save event, answer "got it"   (≈ 0.1 s)
 ② Router:
      • rule "mentions in #support → Support agent" ✓
      • who is Priya? Her Slack account is linked to her BOSS account ✓
      • her thread is new → new conversation
      • guards: not a bot, not a loop, under limits ✓
 ③ Waiting run created for the Support agent, acting for Priya
 ④ A Support-agent copy with a free slot claims it                              (< 0.5 s total)
 ⑤ Agent posts "Looking into it…" in Priya's thread                             (< 1.5 s total)
 ⑥ Agent reads logs and the run history (tools, through the gateways)
 ⑦ Agent edits the status into its answer in the thread
 ⑧ Priya replies in the thread, "can you rerun it?". This continues the SAME conversation:
    no new @mention is needed, and the agent remembers what came before.
```

**If Priya's Slack account isn't linked yet,** the agent doesn't run. Priya gets a private message with a one-time sign-in link. Once she signs in, the link is made and later messages work. If the Slack workspace uses the same Okta SSO as BOSS, an admin can turn on automatic linking by email (section 5).

**Teams works the same way.** A Teams chat or channel thread maps to one conversation, and replies go back into it.

### Flow 2: a ServiceNow incident wakes an agent, with no person involved

A monitoring tool creates incident INC0012345, priority 1, in ServiceNow at 03:12.

```
 ① ServiceNow (a small business rule) ──► BOSS front door: signed ✓, saved, "got it"
    (BOSS also polls ServiceNow every few minutes as a safety net. A duplicate
     is recognised and dropped.)
 ② Router:
      • rule "new P1/P2 incidents → Incident agent" ✓
      • no person caused this → the agent acts for the rule's OWNER, e.g. the
        on-call lead who set the rule up, within the permission they granted
      • guards: at most 5 runs per incident per day; spend cap OK ✓
 ③ Waiting run for the Incident agent; a copy claims it
 ④ Agent investigates (logs, recent deploys, runbooks) and writes its findings
    as a ServiceNow work note, through the MCP gateway
 ⑤ Agent posts a summary in #incidents on Slack
 ⑥ If it wants to do something risky (roll back a deploy), it asks a person
    for approval first: an Approve / Reject card in Slack, as today
```

**The audit trail reads like a sentence:** *"Incident agent, acting for Alice (rule owner), because ServiceNow INC0012345 was created by integration user svc_monitor."*

### Flow 3: the event reaches an agent in a microVM

This answers your question about how events reach agents in remote microVMs.

**Events are not pushed into VMs through `boss-hostd`.** The VM pulls its work, the same way it does everything else.

```
 BOSS control plane                         Host B                          Replica VM (Support agent #2)
 ──────────────────                         ──────                          ─────────────────────────────
                                                                            agent loop: "any work for me?"
                                            boss-hostd ◄──── vsock ──────── (a long-poll, held open)
 run-plane API ◄──── mutual TLS ─────────── forwards it, adding the
   (holds the request open)                 VM's identity
        │
 ③ a waiting run is saved for the Support agent
   → Postgres wakes the waiting request at once
        │
   "here is run 77 for you" + lease, epoch 1 ─────────────────────────────► agent starts run 77
                                                                            replies, tool calls, saves:
                                                                            all go back the same way
```

**The routing is safe:**
- **The right agent only.** A VM can only claim runs for its own agent, because the host's identity says which agent the VM belongs to. A local worker never takes runs meant for a microVM agent.
- **Free slots only.** A VM asks only when it has room. Busy VMs don't take more work.
- **Failover is automatic.** If the VM dies mid-run, its lease runs out after 30 seconds and the other copy resumes the run from its last saved step. Section 7 of the hosts doc covers this.
- **Exactly one answer.** If the dead VM comes back and tries to reply, the reply is refused (fenced). The person never sees two answers.

**Speed targets:**

| Step | Target |
|---|---|
| Event arrives → "got it" sent back | under 0.3 s |
| Event → agent starts working (warm copy) | under 0.55 s |
| Event → first visible "Looking into it…" in Slack or Teams | under 1.5 s |
| Event → agent starts, when no copy was running | under 3 s |

**When every copy is busy,** the run waits in the queue, and the person sees "Queued, 2 ahead of you". If the queue keeps growing, BOSS starts another copy, up to the agent's maximum. Restoring a VM from a snapshot takes milliseconds.

**Chat agents always keep at least one copy running,** and two in production, so people never wait for a cold start. Agents that only handle records or emails may scale to zero, because a 3-second start is invisible there.

### Flow 4: things go wrong

| What happens | What BOSS does |
|---|---|
| Slack sends the same event 3 times | The first copy is kept; the other two are dropped. One run. |
| The BOSS API crashes right after saying "got it" | The event was saved first, so the other API instance processes it. Nothing is lost. |
| An agent's run keeps failing on one event | Retries with growing gaps, then the event moves to the dead-letter list and the rule's owner is told. It can be replayed after a fix. |
| A monitoring tool goes crazy: 3,000 alerts a minute | The storm guard switches that rule to **digest mode**. Instead of 3,000 runs, the agent gets one run every 5 minutes with a summary. Other connections are unaffected. |
| The agent's reply hits Slack's speed limit | The outbox waits as Slack asks, then sends. |
| A Gmail watch or Outlook subscription expires | Renewal runs well before expiry. If it is lost anyway, BOSS recreates it and catches up from its last position, so nothing is missed. |
| The VM running the agent dies mid-conversation | The other copy resumes from the last saved step. The person gets exactly one answer. |

---

## 5. Who the agent acts for

The rule from the hosts design still applies: **the agent never pretends to be a person.** It acts on someone's behalf, with only the overlap of that person's rights, the agent's limits and the task.

**Each rule picks who that person is:**

| Setting | The agent acts for | Used for |
|---|---|---|
| **The person** | Whoever sent the message. No linked account means no run. | Slack and Teams conversations (the default) |
| **The person, or else the owner** | The linked person if there is one, else the rule's owner | Jira or GitHub comments from people |
| **The owner** | The person who set up the rule, within the permission they granted | Events with no person: ServiceNow rules, Salesforce changes, alerts, webhooks |
| **The owner, read-only** | The owner, but with every "write" tool removed | Optional: letting unknown people ask read-only questions |

**How a Slack or Teams user is matched to a BOSS person:**
1. **An existing link,** made once with a one-time sign-in code. This works everywhere.
2. **Automatic by email,** only if an admin confirms the workspace signs people in through the same SSO as BOSS, e.g. Okta. Guests and people from other companies are never matched automatically.
3. **For Teams,** if BOSS's sign-in is Microsoft Entra in the same company, the match uses the person's Microsoft ID directly. That is the strongest match.
4. **Email senders** count as a person only if the email passes the domain's anti-spoofing check (DMARC). Otherwise anyone could pretend.

**When someone leaves the company** and is removed in Okta, their link stops working at the next message.

**Why this matters.** In Microsoft Copilot Studio, event-triggered agents run on the credentials of whoever built them. BOSS re-checks the owner's permission at every firing, and runs conversations as the person who is talking.

---

## 6. Safety guards

**1. No loops.** An agent replying in a channel it listens to must not wake itself up. There are three layers of protection:
- Each adapter recognises messages written by our own bot.
- Every message BOSS writes carries an invisible marker naming the agent and run, and events with our own marker are ignored.
- A hop counter stops agent-to-agent ping-pong at 3 hops.

**2. Per-record limits.** At most 5 runs per ServiceNow incident or record per day, which is ServiceNow's own default for its agents, and 30 per hour per chat thread.

**3. Storm guard.** A sudden flood switches the rule to digest mode (Flow 4).

**4. Spend caps.** Every rule and every agent can have a daily and monthly budget. When it is reached, the rule pauses with a clear reason, and chat users get one line: *"This assistant has reached today's budget."* Before you switch on a busy rule, such as "every message in #support", the console shows an estimate of the daily cost.

**5. Quiet hours.** Record events can wait until morning. Conversations a person starts are always answered.

**6. Untrusted text is screened.** Message and ticket text could contain hidden instructions ("ignore your rules and…"). BOSS already screens ticket text this way. Flagged text is marked, and the agent's next risky action after reading it needs a person's approval.

**7. Kill switch.** Any rule or agent can be paused at once, and BOSS's existing global stop still works.

---

## 7. How others do it

| Product | How their agents wake up |
|---|---|
| **Microsoft Copilot Studio** | Connector "event triggers" (new email, new file, row changed). The event is treated like a user message. Runs on the maker's credentials. |
| **Salesforce Agentforce** | Record changes and platform events, through Flow. Agents publish events back. |
| **ServiceNow AI Agents** | Table and condition triggers, a run-as identity, a cap of runs per record, a kill switch. |
| **Zapier** | Polling every 1 to 15 minutes, with duplicates removed by ID, or webhooks with polling as a fallback. |
| **n8n** | Webhook or polling triggers. |

**The patterns we copy, which everyone serious uses:**
- check the signature before reading anything;
- save first, say "got it", then work;
- remove duplicates using the source's own event ID;
- assume events can arrive twice or out of order;
- renew subscriptions early and catch up after any gap;
- never trust a free-text email as an identity;
- ignore your own bot's messages;
- cap runs per record and per day.

**Where BOSS is stronger:**
- the owner's permission is re-checked at every firing;
- conversations run as the person, not the maker;
- the agent never holds the bot tokens;
- nothing is lost or doubled when a VM fails.

---

## 8. Why we chose this design

**1. Postgres inbox, not Kafka**
- **Instead of:** running Kafka, or another message bus, inside BOSS.
- **Why:**
  - Kafka is another cluster to run, secure and keep highly available.
  - Agent runs are expensive, so the rate of events that become runs is low. Postgres handles it comfortably: about 9 small writes per routed event, so even 100 events a second is within budget.
  - It also gives us one place for duplicates, replay and audit.
- **Later:** Kafka, SQS and Pub/Sub plug in as adapters (sources), not as the backbone.

**2. Pull, not push**
- **Instead of:** pushing events into VMs through `boss-hostd`.
- **Why:**
  - Nothing is lost when a VM dies.
  - Load balances itself.
  - No new way into the VM.
  - The same path for local and microVM agents.
  - It reuses machinery that already exists: Postgres wake-up notifications, leases and fencing.
- **Later:** scaling up and down is just "how many copies are asking".

**3. Intake inside the BOSS API**
- **Instead of:** a separate intake service.
- **Why:** the API already serves BOSS's signed public webhooks, and from phase P8c it runs as two instances. A separate service would be one more thing to make highly available, and one more holder of secrets.
- **Later:** the intake can be lifted into its own service without changing the data.

**4. One adapter contract for every source**
- **Instead of:** wiring each system separately.
- **Why:** one set of guarantees (verify, dedupe, replay, audit) for all sources. Adding ServiceNow or Salesforce means adding an adapter, not a subsystem.
- **Later:** stream sources use the same contract's "subscribe" half. Salesforce's streaming API will be the first, which proves the slot for Kafka.

**5. Replies through the outbox**
- **Instead of:** the agent calling Slack or Teams itself.
- **Why:**
  - Bot tokens never leave BOSS.
  - Speed limits are handled in one place.
  - Failover never produces two answers.
- **Later:** Slack and Teams can also be offered to agents as general MCP tools. The outbox stays the path for conversation replies.

**6. Conversations act as the person**
- **Instead of:** acting as the bot's or the rule-maker's identity.
- **Why:** "what the agent can do for me" is exactly "what I can do" ∩ "what the agent may do". That is the safe default for regulated customers.

---

## 9. What changes from today

| Today | After this work |
|---|---|
| Slack is used only for approvals; mentions of the bot are dropped | Full conversations with agents in Slack threads |
| No Teams | Teams conversations, with proactive messages from agents |
| No ServiceNow, Salesforce or email events | Adapters for each: push where possible, poll as a safety net |
| Each source wired separately; internal events can be lost | One front door, one inbox; nothing lost, nothing doubled; dead letters and replay |
| Up to 15 s before a local agent notices work | Under a second, for local and microVM agents |
| A second message while busy is skipped | Queued, in order, in the same conversation |
| No storm or per-record limits | Digest mode, per-record caps, per-rule budgets, quiet hours |
| Any agent copy could take any run | Runs go only to the right agent's copies, with free slots |

---

## 10. The build plan

```
 P9a ─────────────────────► P9b ──────────────────────────► P9c
 front door, inbox,         Teams, microVM agents,          mail (Gmail, Outlook),
 router, Slack chats,       ServiceNow, GitHub,             Salesforce streaming,
 generic webhook            Jira/Plane, triage,             subscription renewals,
                            autoscaling                     72-hour soak test
 medium-large               large                           medium-large
```

These phases follow the P8 phases they depend on: microVM agents need P8b. P9a works on local agents, so it can start as soon as you approve it.

**P9a: the core, plus Slack conversations**
- **What:**
  - the adapter contract;
  - the front door, inbox, router and dead letters with replay;
  - Slack conversations, including linking by sign-in code or SSO email;
  - the generic signed webhook;
  - sub-second pickup for local agents;
  - the reply outbox with "Working on it…" status edits;
  - the console screens: connections, rules, event stream, dead letters, people links.
- **Done when:**
  - a person holds a 10-turn conversation with an agent in a Slack thread on boss-1;
  - "got it" is answered under 0.3 s at 100 events a second;
  - no events are lost and none are duplicated when the API is killed at random;
  - loops stop;
  - storms switch to digest mode.

**P9b: Teams, microVM agents and SaaS**
- **What:**
  - Teams conversations;
  - microVM agents claiming events through the run plane;
  - autoscaling on queue length;
  - ServiceNow (push plus polling safety net);
  - GitHub and Jira/Plane moved onto the new intake;
  - model triage with cost caps;
  - per-rule budgets and quiet hours.
- **Done when:**
  - Slack and Teams conversations work with an agent running in a microVM on two hosts;
  - killing the VM mid-conversation still gives exactly one answer;
  - no Slack or Teams token is found in any VM.

**P9c: mail, Salesforce, subscriptions**
- **What:**
  - Gmail and Outlook, with subscription renewal and catch-up;
  - Salesforce streaming;
  - the Fleet doctor learns to fix lost subscriptions and dead pollers.
- **Done when:** a 72-hour soak test with all sources loses nothing, duplicates nothing, and keeps "got it" under 0.3 s.

**After the console wireframes are approved,** I'll build P9a.

---

## 11. Decisions we need from you

My recommendation is given for each one.

1. **One Slack app for the whole BOSS install,** fronting many agents, rather than one app per agent. **Recommended: one per install.** The agent is chosen by channel, thread or name ("@BOSS support: …"). Branded per-agent apps can come later.
2. **Slack "Socket Mode"** for installs without a public web address, like a laptop. **Recommended: yes, as an option.**
3. **Automatic linking by SSO email.** **Recommended: only when an admin confirms the workspace uses the same SSO (Okta)**, and never for guests.
4. **People with no linked account.** **Recommended: no run, just a link prompt.** A read-only option can be enabled per rule.
5. **Teams setup.** Microsoft requires a bot registration in Azure, single-tenant, in each customer's own Microsoft tenant. New multi-tenant bots can't be created after 31 July 2025. **Recommended: accept this; the console walks the admin through it.**
6. **Chat agents never scale to zero.** **Recommended: yes**, at least one copy, and two in production.
7. **No silent fallback** from microVM to local when no VM is free. **Recommended: never by default.** It would quietly break the isolation promise.
8. **Model triage off by default.** **Recommended: yes**, with caps when it is turned on.
9. **How long to keep event content.** **Recommended: raw content 14 days, summaries 30 to 90 days.** Please check this against customers' data rules, because emails and messages can contain personal data.
10. **Service events act for the rule's owner,** re-checked at every firing. **Recommended: yes.** Please confirm this model is acceptable.

---

## 12. Glossary

- **Adapter.** A plug-in that knows how to talk to one outside system.
- **Dead letter.** An event that failed repeatedly and was set aside for a person to look at.
- **Digest mode.** During a flood of events, one summary run every few minutes instead of one run per event.
- **Event inbox.** The Postgres table where every incoming event is saved once.
- **Fencing.** Refusing writes from a stale copy of a run, so nothing happens twice.
- **Front door (intake).** The public address that checks, saves and acknowledges events.
- **Lease.** The right to work on a run, held by one agent copy at a time.
- **Long-poll.** A request held open until there is work, so it is answered instantly when work arrives.
- **Outbox.** Where replies wait until they are delivered to Slack, Teams and the like.
- **Poll.** BOSS asking a system "anything new?" on a timer.
- **Push / webhook.** A system calling BOSS when something happens.
- **Router / rule.** Decides which agent handles which event, and for whom.
- **Socket Mode.** Slack connecting to BOSS over an outgoing connection, so BOSS needs no public address.
- **Subscription.** A standing request ("tell me about new mail") that must be renewed before it expires.
- **Triage.** A cheap model call that picks the right agent for a messy input.
- **Waiting run.** An agent run saved in Postgres, ready for the first free copy of the agent to claim.
