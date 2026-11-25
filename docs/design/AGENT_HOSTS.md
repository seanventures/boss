# BOSS Agent Hosts: running agents in microVMs

**What this is:** the design, explained for people. It covers what we are building, how it works and why.
**Status:** for your review. Nothing in it is built yet.
**Date:** 14 November 2026
**Engineering detail:** [`AGENT_HOSTS_LLD.md`](AGENT_HOSTS_LLD.md), in the same folder. That is the low-level design the build agents work from: code references, every test case, the full provider research. You don't need it to understand the design.

---

## How this document is organised

It tells one story, from the problem to the plan.

1. **The problem.** What is wrong today, and what we want.
2. **The big picture.** One diagram of the whole system.
3. **The components.** Every box in the diagram, one at a time.
4. **The flows.** Five step-by-step walkthroughs:
   - deploying an agent;
   - an outside caller calls the agent;
   - the agent calls a model and a tool;
   - a coding task runs;
   - something breaks.
5. **Identity and secrets.** Who holds what.
6. **IP addresses.** Where they come from.
7. **Reliability.** What can fail, and what happens then.
8. **Performance and capacity.**
9. **Why we chose this design.**
10. **What changes from today.**
11. **The build plan.**
12. **Decisions we need from you.**
13. **Glossary.**

If you read only one part, read sections 2, 3 and 4.

---

## 1. The problem

### 1.1 What happens today

When you deploy a BOSS agent today, it runs inside a **BOSS worker container**. That container also holds the database connection and every credential BOSS has. So the agent, which runs model-written code and reads untrusted text, sits right next to the most sensitive things in the system.

BOSS also has a **Firecracker microVM lane** that was meant to give strong isolation. It has four problems:

1. **It is frozen.** It is off by default.
2. **It runs one task at a time per host.** It cannot run hundreds of agents.
3. **It sends secrets into the VM.** The git token and the model API keys are copied into the VM. So the lane with the strongest wall is the one that leaks the most secrets.
4. **Outside callers cannot reach it.** No one outside BOSS can call an agent inside a VM directly.

### 1.2 What we want

These are your requirements:

1. **Choose where each agent runs.** When you deploy an agent, you pick one of two runtimes:
   - **local**: the BOSS worker container, as today. For laptops and testing.
   - **microVM**: a Firecracker VM on a dedicated Linux host. For production and untrusted work.
2. **One lightweight Go service on each host** manages hundreds of microVMs.
3. **No database on the hosts.** BOSS's Postgres stays the only source of truth.
4. **An always-on management agent** watches the fleet and heals it.
5. **High availability in production.** Every production agent runs on at least two hosts, and both are active.
6. **Outside callers can call agents directly**, using certificates, over HTTP and streams.
7. **Routing at the network layer, with no application load balancer.** Each agent has one virtual endpoint. Traffic is spread round robin across the agent's VMs on different hosts.
8. **Each VM's network interface is registered behind its agent's endpoint.**
9. **Agents reach models, tools and APIs outside the VM.** The VM itself holds nothing but the agent.
10. **Secrets are hardened on the host.** No real key ever enters a VM.

### 1.3 What we are not doing

- We are not removing the local runtime. Laptops and tests keep using it.
- We are not building Kubernetes support now. The design leaves room for it.
- We are not running models on these hosts. Models stay with the providers.

---

## 2. The big picture

```
            OUTSIDE CALLERS                                   PEOPLE AND BOSS ITSELF
   (other companies' agents, apps, scripts;                 (console, workflows, Ask BOSS,
    they present certificates)                               MCP clients, schedules)
                  │                                                    │
                  │ agent-x.agents.example.com                         │
                  │ = the agent's VIP, e.g. 203.0.113.17               │
                  ▼                                                    ▼
     ┌───────────────────────────────┐              ┌────────────────────────────────┐
     │  DIRECTOR PAIR (2 machines)   │              │  BOSS CONTROL PLANE            │
     │  Linux IPVS + keepalived      │              │  • BOSS API (2 instances)      │
     │  spreads new connections      │              │  • Identity / token service    │
     │  round robin across the VMs   │              │  • Scheduler                   │
     └───────────────┬───────────────┘              │  • Run-plane API               │
                     │ (later: the routers do       │  • Postgres (source of truth)  │
                     │  this via BGP, and the       └───────┬────────────────────────┘
                     │  directors go away)                  │ desired state + heartbeats
                     │                                      │ (mutual TLS)
         ┌───────────┴─────────────┐          ┌─────────────┴───────────────┐
         ▼                         ▼          ▼                             ▼
 ┌─────────────────── HOST A ────────────────────┐  ┌─────────────────── HOST B ────────────────────┐
 │ boss-hostd (Go)                               │  │ boss-hostd (Go)                               │
 │  • starts / stops / watches the VMs           │  │  • same as Host A                             │
 │  • guest gateway: the VMs' only door out      │  │                                               │
 │  • holds the tokens for its VMs               │  │                                               │
 │                                               │  │                                               │
 │  ┌──────────────┐  ┌──────────────┐           │  │  ┌──────────────┐  ┌──────────────┐           │
 │  │ REPLICA VM   │  │ PER-RUN VM   │  ...      │  │  │ REPLICA VM   │  │ PER-RUN VM   │  ...      │
 │  │ agent-x #1   │  │ coding task  │           │  │  │ agent-x #2   │  │ coding task  │           │
 │  │ IP: inbound  │  │ NO network   │           │  │  │ IP: inbound  │  │ NO network   │           │
 │  │ traffic only │  │ card at all  │           │  │  │ traffic only │  │ card at all  │           │
 │  └──────┬───────┘  └──────┬───────┘           │  │  └──────┬───────┘  └──────┬───────┘           │
 │         └──── vsock ──────┴──► guest gateway  │  │         └──── vsock ──────┴──► guest gateway  │
 └───────────────────────────────────┬───────────┘  └───────────────────────────────────┬───────────┘
                                     │  every call a VM makes leaves this way            │
                                     └────────────────────────┬─────────────────────────┘
                                                              ▼
                     ┌───────────────────────────────────────────────────────────────┐
                     │  CENTRAL GATEWAYS (each one is a pair)                        │
                     │  • Model gateway       → Anthropic, OpenAI, Bedrock …         │
                     │  • MCP / tool gateway  → Jira, ServiceNow, internal APIs …    │
                     │  • Egress gateway      → GitHub, package registries, web      │
                     │  Only these hold the REAL keys (read from the BOSS vault).    │
                     └───────────────────────────────────────────────────────────────┘

     FLEET DOCTOR: an always-on BOSS agent that watches everything above
     and fixes what the simple watchdogs cannot.
```

**The picture in five sentences:**
1. Outside callers reach an agent through its own virtual IP. The directors spread their connections across that agent's VMs on different hosts.
2. Each host runs one Go service, `boss-hostd`, which runs the VMs and is their only door to the outside world.
3. VMs never open network connections out. Every call they make goes over a private host channel (vsock) to `boss-hostd`, and from there to the central gateways.
4. Only the central gateways hold real keys. A VM holds only placeholders that are useless anywhere else.
5. Postgres is the only source of truth, and the hosts keep no database.

---

## 3. The components

Each component gets three short answers: **what it is**, **what it does** and **why we need it**.

### 3.1 BOSS control plane

**What it is.** The BOSS you already have: the API, the console, Postgres and the identity service. It runs as two API instances.

**What it does here:**
- **Decides what runs where.** The scheduler places each agent's VMs on hosts, never two copies of one production agent on the same host.
- **Tells hosts what to run.** Each host watches its own "desired state" list and makes reality match it.
- **Serves the run-plane API.** Agents inside VMs use it to:
  - start runs;
  - save progress (checkpoints);
  - ask for approvals;
  - reserve budget and write the ledger.

  VMs never touch the database directly.
- **Issues identities, tokens and certificates** for hosts, agents and runs.

**Why we need it.** One place owns the truth. Hosts and VMs can crash or be replaced without losing anything important.

### 3.2 Postgres

**What it is.** BOSS's existing database.

**What it does.** It stores every deployment, every VM's state, every run, checkpoint, approval and audit record. It also holds each run's **lease**: which VM is allowed to work on that run right now. Flow 5 shows why that matters.

**Why we need it.** It is the single source of truth. Today it is also a single point of failure. Phase P8d adds an automatic standby.

### 3.3 The Linux host

**What it is.** A Linux machine with hardware virtualisation (KVM). A typical production host has 64 cores, 256 GB of memory and NVMe disks. boss-1 is a small one: 8 cores and 16 GB.

**What runs on it:**
- `boss-hostd`;
- Firecracker and its "jailer";
- shared read-only VM images and snapshots;
- optionally, a local package cache for npm, Maven and pip.

**What does not run on it:** no database, no customer tools and no real API keys.

### 3.4 `boss-hostd`: the host service

**What it is.** One small Go program per host. It grows out of today's `sandbox-service`, and it is the only thing on the host that BOSS talks to.

**It has four jobs:**
1. **VM manager.** It starts, stops, snapshots and restores VMs. It keeps a warm pool ready, and creates a VM's network interface when one is needed.
2. **Watchdog.** It pings every VM every 500 ms over vsock. A dead VM is noticed instantly if its process exits, or within 1.5 s if it hangs. The VM is then pulled out of traffic in under a second and restarted.
3. **Guest gateway.** Every call a VM makes arrives here. The gateway checks the call, attaches the right token and forwards it to a central gateway. Flow 3 walks through this.
4. **Reporter.** It sends BOSS a heartbeat every few seconds with:
   - health and capacity;
   - proof of its security settings: Firecracker live, jailer on, strict-secrets mode on.

**Its memory.** A small cache file of what it is running. It is never the truth. If it is lost, the host rebuilds it from BOSS plus what is actually running.

### 3.5 The microVMs

**What a microVM is.** A real virtual machine with its own Linux kernel, made tiny and fast by Firecracker, the engine AWS built for Lambda. It boots in about 125 ms with about 5 MB of overhead. Because each VM has its own kernel, one VM cannot see another VM or the host. That is much stronger than a container.

**There are two kinds of VM. This is the most important idea in the design.**

> **Operator decision (14 Nov 2026):** only **replica VMs** are switched on. Per-run VMs are built in full and tested end to end, but they stay **off by default**, as the coding lane is today. Anyone who wants them can switch them on. The priority is independent agents that run autonomously and do their job, not coding tasks.

```
 REPLICA VM: a long-lived copy of an agent that callers talk to
 ┌──────────────────────────────────────────────────────────┐
 │  agent runtime: TLS listener, agent loop, tools          │
 │                                                          │
 │  network card ◄── ONLY callers' connections coming in    │ ← one private IP, e.g. 10.200.4.7,
 │                   and the replies going back             │   plus the agent's VIP, hidden inside
 │                                                          │
 │  vsock ─────────► everything the agent calls out to      │ ← models, tools, git, BOSS
 └──────────────────────────────────────────────────────────┘

 PER-RUN VM: a throwaway VM for one coding task
 ┌──────────────────────────────────────────────────────────┐
 │  coding agent (Claude Code / Codex CLI) + toolchains     │
 │                                                          │
 │  NO network card at all                                  │ ← no IP, no DNS, nothing
 │                                                          │
 │  vsock ─────────► everything the agent calls out to      │ ← models, tools, git, BOSS
 └──────────────────────────────────────────────────────────┘
```

| | Replica VM | Per-run VM |
|---|---|---|
| Used for | Chat agents, API agents, bots, agents called from outside | Coding tasks; untrusted or public work that changes things |
| Lives for | Days. Replaced only on upgrade or failure. | One run, then destroyed |
| Runs at once | Several runs (4 by default), each with its own user and folder | One run |
| Has an IP? | Yes, one private IP, for incoming traffic only | No |
| Reaches the outside through | vsock → `boss-hostd` | vsock → `boss-hostd` |

**What "incoming traffic only" means.** The host firewall on a replica's network interface allows exactly two things:
- callers' packets coming in to the agent;
- the agent's replies going back.

Anything the VM tries to start on its own, such as a connection to the internet or to another VM, is dropped and counted. A count above zero is a security alarm.

**What vsock is.** A private channel between one VM and its own host. It is not a network: no IP addresses, no routing, no DNS. On the host, each VM's vsock is a separate socket. So `boss-hostd` always knows for certain which VM sent a message, and no VM can pretend to be another.

**What is inside a VM:**
- the BOSS agent loop, the same code as the local runtime;
- for replicas, a TLS listener serving the agent's API (Agent Protocol and A2A);
- a tiny relay that forwards local calls (`127.0.0.1:71xx`) over vsock to the host;
- for coding VMs, the toolchains and the Claude Code and Codex CLIs.

### 3.6 Images, snapshots and the warm pool

**Images.** Every VM starts from a signed, read-only disk image. There are two:
- **agent** (about 250 MB): just the agent runtime;
- **coder** (about 1.5 GB): Java, Node, Python, git and the coding CLIs.

All VMs on a host share the same image. Each VM gets only a small private writable layer on top, so starting a VM copies nothing. A host refuses to boot an unsigned image.

**Snapshots.** A cold boot plus loading the runtime takes 1.5 to 3 seconds. So the host boots one VM once, waits until it is fully loaded, and saves its memory and disk as a **snapshot**. New VMs restore from it in roughly 10 to 40 ms.

**One safety rule.** A snapshot is taken only *before* the VM has any identity, token or run, or every copy would carry the same secrets. After restore, each VM gets its own fresh identity and random seed.

**Warm pool.** The host keeps a few per-run VMs restored and waiting (2 by default). A coding task gets one in under 300 ms.

**Per-repository setup cache.** Installing a repo's dependencies can take minutes. The first run for a repo does it once, and the result is saved and reused by later runs. It is thrown away when the lockfile changes, or after 7 days. Claude Code on the web and OpenAI Codex do the same.

### 3.7 The director pair (IPVS + keepalived)

**What it is.** Two small Linux machines in front of the hosts. In small installs they can share the BOSS API machines.

**What it does.** It owns the agents' virtual IPs. For each new connection to a VIP, it picks one of that agent's VMs, round robin, and forwards the packets there. It uses two standard parts of Linux:
- **IPVS**, the load balancer built into the Linux kernel; Kubernetes uses it too. It looks only at addresses and ports, and it never decrypts traffic.
- **keepalived**, which keeps each VIP on a live director. If director A dies, director B takes its VIPs in about 3 seconds.

A small Go program, `boss-lbd`, sets up IPVS from BOSS's desired state. It also accepts "remove this VM now" messages straight from the hosts, so a dead VM leaves traffic in milliseconds.

**Both directors are active.** Half the VIPs live on A and half on B, and each backs up the other.

**Later (phase P8e), the directors go away.** Each host tells the network routers, using BGP, "I can serve this VIP". The routers spread connections across hosts themselves (ECMP), and drop a dead host in 50 to 300 ms. This needs routers that speak BGP, which is normal in a data centre.

**In public clouds** you usually cannot float IPs or speak BGP. There, the host registers its VMs with the cloud's own layer-4 load balancer. It is still network-layer, with no application proxy.

### 3.8 The central gateways

**What they are.** Three small services, each run as a pair:
1. **Model gateway.** Calls Anthropic, OpenAI, Bedrock and others. It holds the real API keys, enforces each run's budget and allowed models, and streams replies without buffering.
2. **MCP / tool gateway.** Calls MCP servers and APIs such as Jira, ServiceNow, Salesforce and internal services. It holds those credentials and refreshes OAuth tokens.
3. **Egress gateway.** Handles git, package downloads and allowed web calls. It turns the run's placeholder into a GitHub token that works only on that run's repositories.

**Every call goes through the same five steps:**
1. Check the run's token: who is calling, for which run, and is it still valid?
2. Check policy: is this host, path and method allowed for this run?
3. Swap in the real credential.
4. Forward the call.
5. Write an audit record.

**Why central.** Real keys live in two processes per gateway, not on hundreds of hosts. Rotating a key is one vault update. A stolen host holds no keys.

### 3.9 Identity service and vault

These already exist in BOSS. The **vault** holds the real keys, and only the central gateways read it. The **identity service** mints the short-lived run tokens (section 5).

### 3.10 The Fleet doctor

**What it is.** An always-on BOSS agent with two copies. It runs on the local runtime, so it is never inside the fleet it repairs.

**What it does.** It handles problems that need judgement:
- a new image that crashes everywhere;
- a host running out of disk;
- a gateway that keeps refusing calls;
- one agent whose VMs keep restarting.

It reads metrics, logs and audit records, finds the cause, and proposes or applies a fix. Risky fixes go to a person.

**Self-healing has two layers:**

| | Layer 1: watchdog (in `boss-hostd`) | Layer 2: Fleet doctor |
|---|---|---|
| Speed | Milliseconds to seconds | Minutes |
| Handles | "This VM died": withdraw, restart, replace | "Why does it keep dying?" |
| Decides by | Fixed rules | Reasoning plus tools |
| Works when BOSS is down? | Yes | No |

---

## 4. The flows

### Flow 1: deploying an agent

You deploy `agent-x` to production with `runtime: microvm` and `replicas: 2`.

```
 You (console)          BOSS control plane                Host A and Host B            Directors
    │ deploy agent-x        │                                   │                          │
    │──────────────────────►│ 1. save the deployment            │                          │
    │                       │ 2. give agent-x a VIP and a name  │                          │
    │                       │ 3. place copy 1 on A, copy 2 on B │                          │
    │                       │──────── desired state ───────────►│                          │
    │                       │                                   │ 4. restore VM (~40 ms)   │
    │                       │                                   │ 5. give it an IP, a fresh│
    │                       │                                   │    identity and seed     │
    │                       │◄── 6. VM asks for a TLS cert ─────│                          │
    │                       │──────── cert (24 h) ─────────────►│                          │
    │                       │                                   │ 7. health check OK       │
    │                       │                                   │──── 8. "add this VM" ───►│
    │◄──────── agent-x is live at agent-x.agents.example.com ──────────────────────────────│ 9. round robin
```

1. BOSS saves the deployment.
2. BOSS gives the agent a VIP from the pool, plus a DNS name.
3. The scheduler puts the two copies on **different hosts**.
4. Each host restores a VM from the snapshot.
5. The host gives the VM its private IP, a fresh identity and a fresh random seed.
6. The VM creates its own TLS key and asks BOSS, through the host, for a certificate. The certificate lasts 24 hours and renews itself.
7. The host checks the VM is healthy.
8. The host tells the directors to add the VM behind the VIP.
9. The agent is live, and new connections are spread across both copies.

**Upgrades** work the same way. New VMs start and are added. Old VMs finish their current work and are removed. Callers see no outage.

### Flow 2: an outside caller calls the agent

**In one line:** the caller sends packets to the agent's VIP; a director picks a VM and passes the packets to it inside an envelope; the VM answers the caller directly.

```
 Caller 198.51.100.5
    │ ① opens a TLS connection to 203.0.113.17:443
    ▼
 Director A (owns VIP 203.0.113.17 right now)
    │ ② IPVS: new connection → next VM in the list
    │       list for this VIP: 10.200.4.7 (Host A), 10.200.8.3 (Host B)
    │ ③ wraps the packet in an IPIP envelope addressed to 10.200.8.3
    ▼
 Network: "10.200.8.0/23 lives on Host B"
    ▼
 Host B → the VM's network interface (tap) → VM 10.200.8.3
    │ ④ VM opens the envelope. The inner packet is for 203.0.113.17,
    │    which the VM also holds (hidden), so it accepts it.
    │ ⑤ TLS ends here, inside the VM, with agent-x's own certificate.
    │ ⑥ VM checks the caller's certificate, then asks BOSS (through the host):
    │    "may this caller use agent-x?"  → yes, here is a run
    │ ⑦ the agent works and streams its answer
    ▼
 The reply goes straight from the VM to the caller, from address 203.0.113.17.
 It does NOT go back through the director.
```

**② Round robin per connection.** IPVS keeps a list of the VMs behind each VIP. When a new connection arrives, it picks the next VM and remembers the choice. Every later packet of that connection goes to the same VM, so the conversation stays intact. Spreading individual *packets* would break TCP, so nobody does that.

**③ IPIP, the envelope.** The director does not change the caller's packet. It puts the whole packet inside a new outer packet addressed to the chosen VM:

```
 Original:  [ to 203.0.113.17 | from 198.51.100.5 | encrypted data ]
 Wrapped:   [ to 10.200.8.3 | from director ] [ to 203.0.113.17 | from 198.51.100.5 | encrypted data ]
              └── outer envelope ──┘            └──────────── original, untouched ────────────┘
```

The envelope is ordinary IP, so it crosses any routed network. The directors only need to know which host owns which block of addresses.

**④ Why the VM accepts it.** Every VM of the agent also holds the VIP, on a hidden interface that never answers "who has this address?" on the network. So the VM accepts packets for the VIP but never competes with the director for it.

**Why replies skip the director.** Requests are small, and answers, especially streamed model output, are large. The directors carry only the small half, so a modest pair can front a very large number of agents.

**⑤ TLS ends inside the VM.** Nothing in between can read the traffic, not even the directors. Each agent has its own certificate from BOSS. Callers trust BOSS's agent certificate authority, or a public certificate, if you choose one for that agent.

**⑥ The VM does not decide alone.** BOSS's policy engine decides whether the caller may use the agent. A hacked VM cannot grant itself permissions.

**How internal work and events reach a replica.** VMs never ask BOSS for work themselves. Each host's `boss-hostd` keeps **one** open request to BOSS for all its VMs. The request is empty. BOSS already knows each VM's agent, free slots and health from its own records, and the host reports only changes. When a run arrives, BOSS picks a healthy VM of the right agent on that host and records that the run belongs to that VM. The host then hands the run to that VM over vsock. That is one request per host, not per VM.

**Long conversations.** The next message of a conversation may arrive on a new connection and land on the other VM. That is fine, because the conversation lives in Postgres:
- **The conversation is idle:** the receiving VM takes it.
- **The other VM is mid-answer:** the receiving VM's host forwards the request to that VM's host. VMs never talk to each other directly.
- **The other VM is dead:** its lease expires after 30 s, and the receiving VM resumes from the last checkpoint.

### Flow 3: the agent calls a model and a tool

The VM has no keys and no outbound network. So how does it call Claude, or Jira?

```
 INSIDE THE VM                              HOST                         CENTRAL                  OUTSIDE
 Claude CLI / agent loop
   ANTHROPIC_BASE_URL = http://127.0.0.1:7101
   API key = boss_mdl_abc   (a placeholder, not a real key)
   │ ① calls http://127.0.0.1:7101/v1/messages
   ▼
 local relay ── vsock ──► boss-hostd guest gateway
                           │ ② "this socket is VM 42, which runs run 7"
                           │ ③ "boss_mdl_abc belongs to run 7 and is still valid"
                           │ ④ removes the placeholder; attaches run 7's BOSS token
                           │    (the host holds it; the VM never sees it)
                           │ ⑤ forwards over mutual TLS ───────────► MODEL GATEWAY
                                                                       │ ⑥ checks the token,
                                                                       │    budget and model
                                                                       │ ⑦ adds the REAL key
                                                                       │ ─────────────────────► api.anthropic.com
   ◄──────────────── the reply streams back the same way, chunk by chunk ──┘
 ⑧ each hop writes an audit record: run, VM, destination, allowed or refused
```

**Other calls follow the same path, with a different gateway:**
- **A tool call (e.g. Jira through MCP):** the VM holds a `boss_mcpgw_…` placeholder. The host attaches the run token. The **MCP gateway** checks policy, adds the real Jira credential and calls Jira.
- **git:** the VM's git holds a `boss_egr_…` placeholder. The **egress gateway** swaps it for a GitHub token that works only on this run's repositories, and it blocks dangerous actions such as deleting branches.

**If someone steals the placeholder:**
- **Inside the VM:** it only works through this VM's socket, for this run, to allowed places. That is exactly what the agent could already do.
- **Outside the VM:** it is worthless. No gateway accepts it without the host's token, and the host attaches that token only to calls arriving on the right VM's socket.

**The cost of the extra hop** is about 1 to 2 ms. A model reply takes hundreds of milliseconds to seconds, so the hop is lost in the noise. Streaming passes through chunk by chunk.

### Flow 4: a coding task

Someone asks BOSS to implement Jira ticket LEND-142.

```
 1. BOSS starts a run with the coding workflow
 2. Host A takes a warm per-run VM from its pool               (< 300 ms)
 3. Host A attaches the repo's cached setup layer, if any      (skips npm install)
 4. Host A gives the VM its identity and placeholders, over vsock
 5. The coding CLI inside the VM:
       • clones the repo        → vsock → host → egress gateway → GitHub
       • calls the model        → vsock → host → model gateway  → Anthropic
       • reads the ticket       → vsock → host → MCP gateway    → Jira
       • builds and tests       → locally, inside the VM
       • saves progress         → vsock → host → run-plane API  → Postgres
       • pushes and opens a PR  → vsock → host → egress gateway → GitHub
 6. The run ends and the VM is destroyed. Only what was saved in BOSS
    and pushed to GitHub survives.
```

**Why a fresh VM every time.** One task, or one customer, can never affect the next. With no network card at all, even a broken firewall rule cannot leak anything.

### Flow 5: something breaks

**Two rules make recovery safe:**
1. **Progress is saved as checkpoints in Postgres** after every step.
2. **Only the lease holder may write.** Each run's lease has a number, the "epoch". When a run moves to a new VM, the number goes up, and any write with an old number is refused. So a VM that comes back from the dead cannot do anything twice.

| What breaks | Noticed in | What happens | What callers see |
|---|---|---|---|
| **One VM** | Instantly; 1.5 s if it hangs | Out of traffic in under 1 s and restarted. Its runs resume on another VM from the last checkpoint in about 35 s. | Open connections reset; the next one works |
| **A whole host** | About 2 s | Its VMs leave traffic. Replacements are placed elsewhere after 10 s. Runs resume in about 35 s. | Each agent briefly loses half its capacity, never all of it |
| **`boss-hostd` crashes** | Instantly | Restarts in 1 to 2 s. The VMs keep running. | Calls retry; nothing is lost |
| **One director** | About 3 s | The other director takes over. Open connections survive. | Almost nothing |
| **One gateway instance** | 1 to 3 s | The host uses the other instance. A model stream in flight is retried by the agent. | One slower step |
| **Both instances of a gateway** | 1 to 3 s | Calls that need it fail loudly. There is never a "go direct" fallback, which would leak keys. The Fleet doctor alerts. | A clear error at the next such call |
| **BOSS control plane** | Seconds | Hosts keep running and serving open conversations. New runs wait. | New runs wait |
| **Postgres** | Today: a restart. From P8d: automatic failover in 10 to 30 s | Runs pause at their next save, then continue | A pause |
| **A host cut off from BOSS** | 10 s | Its runs' leases expire after 30 s and resume elsewhere. When the host returns, its old writes are refused. | A pause, never a duplicate |

**The worst case after any single failure** is an action that was half-done when a VM died, such as a `git push` with no recorded result. It is never blindly repeated. It is marked "unknown outcome" and shown to a person.

---

## 5. Identity and secrets: who holds what

### 5.1 The agent has three identities, each with one job

| Identity | What it proves | Held by | Lifetime |
|---|---|---|---|
| **Passport** | "This is agent-x" in BOSS | BOSS | As long as the agent exists |
| **Serving certificate** | "You really reached agent-x", shown to callers | Inside the replica VM | 24 h, auto-renewed |
| **Workload identity** | "This request came from VM 42 on Host B, running signed image X" | The host, never the VM | 1 h, auto-renewed |

The serving certificate is the only identity inside a VM. It can **receive** calls, never **make** them.

### 5.2 Delegation, not impersonation

The agent never pretends to be a person. Every run token names two parties:
- the **subject**: the person the work is for;
- the **actor**: the agent.

What the run may do is the overlap of four things:

```
   what the person may do
 ∩ the agent's own ceiling
 ∩ the grant the person gave
 ∩ what this task needs
 = what this run may do
```

- **Interactive runs:** the person is the caller.
- **Scheduled runs, triggers and bots:** the person is whoever set up the trigger and gave consent. If that person loses access or revokes the grant, the agent's runs stop at the next token refresh.

This is the standard "on-behalf-of" token exchange (RFC 8693) used by Okta, Entra and Google.

### 5.3 How tokens are fetched, refreshed and cached

```
 A run starts
   │
   ▼
 boss-hostd ── token exchange ──► BOSS identity service
   │            "agent-x for person P, run 7, on VM 42"
   │◄────────── run token (≤ 1 h), locked to a key only this host has
   │
   │ before it expires: exchange again → fresh token
   │
   │ one narrower token per destination:
   │   model gateway · egress · MCP · run plane
   │   (a token for one destination is refused by the others)
   │
   └─ all kept in host memory only: never on disk, never in the VM
```

**Why the host and not the VM.** The VM runs model-written code and reads untrusted text: web pages, tickets, emails. A trick hidden in that text ("prompt injection") can steer the agent, so anything the VM can read could be stolen. A token in the host's memory cannot be read from inside the VM.

**"Locked to a key"** (the DPoP standard) means the token works only with a fresh signature from the host's private key. A copied token alone is useless.

### 5.4 Where every secret lives

| Secret | Lives in | Ever inside a VM? |
|---|---|---|
| Anthropic / OpenAI API keys | Vault → model gateway memory | **No** |
| GitHub app key | Vault → egress gateway, which mints short per-run tokens | **No** |
| Jira, ServiceNow and other tool credentials | Vault → MCP gateway, which also refreshes OAuth | **No** |
| Run tokens and workload identity | Host memory | **No** |
| Placeholders (`boss_mdl_`, `boss_egr_`, `boss_mcpgw_`) | The VM | **Yes, and they are worthless outside it** |
| The agent's serving certificate key | Created inside the replica VM | Yes, but it can only receive calls |

### 5.5 Hardening the host

- **Jailer.** Every Firecracker process is locked down: its own empty filesystem view, its own unprivileged user, a short list of allowed system calls, and fixed resource limits. Escaping a VM lands an attacker in a tiny cage, not on the host.
- **Strict-secrets mode.** The host refuses any request that would put a real credential into a VM, and it proves to BOSS that it is in this mode. It is on by default for every microVM host.
- **Clean snapshots.** No secret is ever inside a VM when it is snapshotted.
- **No metadata access.** VMs cannot reach the host's or the cloud's metadata service, a classic route to stealing cloud credentials.
- **Every call is audited.** Each call out of each VM is recorded with its run, VM, destination, result and duration. The Fleet doctor watches these records for anything unusual.

### 5.6 How the big providers do it

We studied Anthropic, OpenAI, AWS, Google, Microsoft, E2B, Daytona, Modal, Cloudflare, Fly and Vercel. **They all follow one rule: a key the sandbox can read is a key the agent can leak.**

- **Models:** the sandbox calls a proxy, and the proxy adds the key.
- **Tools and MCP:** calls go through a gateway that holds the credentials, such as AWS AgentCore Gateway, Google Agent Gateway or Anthropic's MCP connector.
- **git and the web:** an egress proxy swaps placeholders for real tokens. Claude Code on the web, OpenAI Codex, E2B, Vercel and Daytona all do this.
- **Private company tools:** reached over private links. AWS uses a VPC, Codex uses Tailscale, Anthropic uses MCP tunnels.
- **On the host:** nobody installs customer tools. Tools are baked into images, and environments are snapshotted and cached.

**Ours is the high-assurance version of the same pattern.** Anthropic's Agent SDK security guide describes it: a Firecracker VM with no network card, and vsock to a host proxy that adds credentials. Most providers instead intercept the sandbox's encrypted traffic with their own certificate. We don't, because that breaks tools that check certificates, and it puts a powerful signing key on every host.

### 5.7 The one open problem: Claude and Codex subscriptions

API keys work perfectly with this design. **Subscription logins don't yet.** These are:
- the one-year Claude `setup-token`;
- the Claude CLI login, which lasts about 8 hours;
- the Codex ChatGPT login.

The CLI uses these logins directly, so today they sit inside the agent's environment, in every lane.

**The plan:**
1. **On microVMs, use API keys or the LLM gateway.** This works today.
2. **Run a one-day test on boss-1.** Can the CLI run in subscription mode with a placeholder while the model gateway swaps in the real login? Nobody documents this, so we must test it.
3. **Until then, microVM hosts refuse subscription logins.** Those runs stay on the local runtime, with a visible reason.
4. **A business question for you:** do the subscription terms allow automated use across many VMs?

---

## 6. IP addresses

There are three pools, and BOSS assigns every address automatically.

```
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ VIP POOL: one address per production agent                              │
 │   public range, if callers come over the internet                        │
 │      e.g. 203.0.113.0/26  (from your ISP or cloud provider)              │
 │   private range, if callers are only inside your network                 │
 │      e.g. 10.50.0.0/24                                                   │
 │   IPv6 preferred where callers support it. If IPv4 runs short:           │
 │   one shared VIP, a different port per agent.                            │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ HOST VM BLOCKS: one private block per host                               │
 │   Host A 10.200.4.0/23   Host B 10.200.8.0/23   Host C 10.200.12.0/23 …  │
 │   510 addresses each; every REPLICA VM takes one;                        │
 │   per-run VMs take none                                                  │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ INFRASTRUCTURE: your existing server network                             │
 │   hosts, directors, BOSS API, gateways, Postgres                         │
 └──────────────────────────────────────────────────────────────────────────┘
```

- **VIPs.** You give BOSS one or more ranges in settings. An agent gets the next free VIP when it is first deployed to production, and returns it when it is retired.
- **Host blocks.** A host gets the next free block when it joins the fleet. Each new replica VM on that host takes the next free address in the block.
- **Routing stays tiny.** The network needs one route per host ("10.200.8.0/23 → Host B"), never one per VM.
- **In a cloud,** the host's block is attached to its network card with the cloud's "prefix delegation" feature, so VM addresses are routable in the VPC.

---

## 7. Reliability

### 7.1 Targets (please confirm)

| What | Target |
|---|---|
| An outside caller can reach a production agent | 99.95 % per month (about 22 minutes down) |
| A new run can start | 99.5 % until Postgres HA (P8d), then 99.9 % |
| A run in progress survives any single failure | Always. It resumes from its last checkpoint in about 35 s. |

### 7.2 Why these numbers hold

Suppose any one machine is up 99.5 % of the time, which is about 3.6 hours down a month including maintenance. A **pair** is down only if both are down at once: 0.5 % × 0.5 % = 0.0025 %. So any pair is up about 99.9975 % of the time.

- **Reaching an agent** needs its two hosts (a pair) and the two directors (a pair). Together that is about **99.995 %**.
- **Starting a run** also needs Postgres. Today Postgres is a single machine at 99.5 %, so starting a run is about 99.48 % available. With a standby, it rises to about **99.93 %**.

We promise less than the arithmetic says, because real outages are often shared: the same rack, the same bad image, the same network change. The guards against that:
- copies of an agent are spread across racks;
- new images go to one host first;
- a bad image is rolled back automatically;
- the directors use separate network links.

### 7.3 Single points of failure, and when each goes away

| Weak spot today | Fixed in |
|---|---|
| Postgres is one machine | **P8d:** a standby with automatic failover, plus backups |
| The gateways are single instances | **P8b** builds them; **P8c** makes each a pair |
| The BOSS API is one instance | **P8c:** two instances |
| The director pair | **P8e:** removed, because the routers do the spreading |

### 7.4 How we prove it

Each phase runs failure tests while traffic is flowing: kill a VM, kill a host, kill a director, cut a host off from BOSS, kill a gateway. Each test checks:
- that recovery times match the table in Flow 5;
- that nothing happened twice, for example exactly one `git push` after a network split.

---

## 8. Performance and capacity

### 8.1 Speed

| What | How fast | Source |
|---|---|---|
| Firecracker boot | under 125 ms | Published by Firecracker / Anthropic |
| Restore from snapshot | 10 to 40 ms | Our estimate; measured in P8b |
| Get a warm VM for a coding task | under 300 ms | Target |
| Cold start of a new VM type | under 3 s | Target |
| Remove a dead VM from traffic | under 1 s | Target |
| Extra time the host adds to each call | under 2 ms | Target; measured in P8b |
| A model call, for comparison | hundreds of ms to seconds | |

Every estimate has a named benchmark in the build plan. A missed target blocks the phase, or the number is corrected with your agreement. It is never quietly ignored.

### 8.2 How many VMs fit on one host

For a production host with 64 cores and 256 GB:

| VM type | Size | Per host |
|---|---|---|
| Agent replica | 512 MB, 1 core | about **350** VMs, up to 4 conversations each, so about 1,400 at once |
| Coding VM | 4 GB, 2 cores | about **55** VMs |

boss-1 holds about 20 agent VMs, or 3 coding VMs.

Agent VMs pack densely because they mostly wait for the model, so many can share each core. BOSS watches CPU pressure and stops placing VMs when it gets tight. In practice, model rate limits and budgets run out before host capacity does.

### 8.3 What gets tight first, and what we do then

| Component | Gets tight when | Next step |
|---|---|---|
| Hosts | CPU or memory pressure | Add hosts. Joining one is a single step. |
| Directors | Very high connection rates | Move busy agents to BGP (P8e); later, eBPF load balancing |
| Central gateways | Many simultaneous streams | Add instances; then a gateway group per project |
| Postgres | Write rate from checkpoints and audit | Batch writes, add a connection pooler, split tables by time, move big payloads to object storage |

**Postgres feels growth first.** As an estimate, 1,000 runs at once means about 500 small writes a second. One well-tuned Postgres handles that comfortably.

---

## 9. Why we chose this design

Each choice gets four answers: what we chose, what else we considered, why, and how it helps later.

**1. Firecracker microVMs**
- **Instead of:** containers, gVisor or Kata.
- **Why:**
  - Each VM has its own kernel, so a kernel bug in one VM cannot reach the others.
  - A VM starts in milliseconds and costs about 5 MB.
  - It is the industry norm: AWS Lambda, E2B, Fly, Vercel and AgentCore all use microVMs.
  - BOSS already has a working Firecracker backend.
- **Later:** other runtimes, such as Kubernetes or E2B, can plug in behind the same host contract.

**2. Go for `boss-hostd`**
- **Instead of:** Java, Rust or C.
- **Why:**
  - One small binary with low memory, and strong Linux networking libraries.
  - Today's `sandbox-service` is already Go.
  - Java's memory use and start-up time are wrong for a host agent.
  - Rust would be a little faster, but it costs development speed, and this job is mostly waiting and relaying.
- **Later:** the BGP and eBPF libraries we will need are mature in Go.

**3. No database on the hosts**
- **Instead of:** SQLite or Postgres on each host.
- **Why:** one source of truth means no syncing and no conflicts. A host can be wiped and rebuilt in minutes.
- **Later:** adding a host is trivial.

**4. Two kinds of VM**
- **Instead of:** one kind for everything.
- **Why:** chat and API agents must be always on, quick and reachable. Coding tasks need a clean machine every time, with big toolchains. One kind can't do both well.
- **Later:** public, untrusted callers of agents that change things get their own per-run VM, so separate callers never share a machine.

**5. No outbound network for VMs (vsock only)**
- **Instead of:** a network card with an egress proxy, or intercepting encrypted traffic.
- **Why:**
  - It removes whole classes of attack at once: data smuggled out through DNS, reaching other VMs, reaching cloud metadata, connecting to any server.
  - With a network card, every guarantee depends on firewall rules staying perfect forever.
  - vsock tells the host for certain which VM is speaking.
  - Anthropic recommends exactly this as its high-assurance option.
- **Later:** a new tool or API is just a new route on a gateway. A locked-down network card exists as an opt-in for the rare tool that needs raw sockets, but it does not count as strong isolation.

**6. Real keys only in central gateways**
- **Instead of:** keys on every host.
- **Why:** two copies per gateway are easy to protect and rotate; hundreds of hosts are not.
- **Later:** new connectors like ServiceNow and Salesforce plug into the MCP gateway once, and every agent can use them without ever seeing a credential.

**7. Short tokens held by the host and locked to a key**
- **Instead of:** long-lived tokens in the VM.
- **Why:**
  - A stolen token is useless without the host's key.
  - Permissions are always the overlap of person, agent and task.
  - These are open standards.
- **Later:** it fits enterprise identity, including the Okta SSO you set up, and other companies' agents calling ours.

**8. Network-layer routing (IPVS, then BGP)**
- **Instead of:** an application load balancer such as nginx or Envoy, or DNS round robin.
- **Why:**
  - It is your requirement.
  - Encryption stays end to end with the agent.
  - The kernel forwards packets far faster than any proxy.
  - Replies skip the director.
  - DNS round robin cannot drop a dead VM quickly, because callers cache DNS answers.
- **Later:** BGP removes the directors entirely, and clouds get the same model through their layer-4 load balancers.

**9. TLS ends inside the VM**
- **Instead of:** ending TLS at a front proxy.
- **Why:** the agent proves its own identity, and nothing in between can see the data. Banks and other regulated customers need this.

**10. Leases and fencing**
- **Instead of:** trusting that a dead VM is really dead.
- **Why:** networks split, and "dead" VMs come back. Fencing makes a duplicate action impossible, not just unlikely.
- **Later:** this is what lets failover be fast and aggressive safely.

**11. Snapshots and warm pools**
- **Instead of:** cold boots.
- **Why:** start-up drops from seconds to milliseconds, and VMs restored from one snapshot share memory.
- **Later:** the per-repo setup cache reuses the same mechanism.

**12. Two layers of self-healing**
- **Instead of:** only rules, or only an AI agent.
- **Why:** rules are fast and predictable for the common case. An agent is better at the rare, messy case. The rules keep working even when BOSS is down.

---

## 10. What changes from today

| Today | After this work |
|---|---|
| Firecracker lane frozen, one task per host | Hundreds of VMs per host, chosen per deployment |
| Git token and model keys copied into the VM | Placeholders only; real keys stay in the central gateways |
| Agents reachable only through BOSS | Each production agent has its own VIP and certificate |
| One shared static password between BOSS and hosts | Certificates per host, revocable |
| One shared network interface for all VMs | Coding VMs: no network. Replicas: their own incoming-only interface. |
| Cold boot and a full disk copy per task | Shared images, snapshots, warm pools, setup cache |
| Postgres on one machine | Standby with automatic failover (P8d) |
| "Strong isolation" hard-coded to *no* | A checked fact, true only on verified microVM hosts. This unlocks outside calls to agents that change things. |

---

## 11. The build plan

```
 P8-pre ──► P8a ──────► P8b ──────────► P8c ──────► P8d ─────────────► P8e
 fix today's the        agents in VMs,  high        production         BGP,
 leaks       skeleton   reachable at    availability hardening +       no directors
                        their VIPs                  Postgres HA
 small       medium     large           medium      medium             medium
```

Each phase must pass its tests on boss-1 and on a laptop before the next phase starts. Each phase has its own security, reliability and speed targets.

**P8-pre: fix today's leaks (small, BOSSAPP-1061)**
- **What:** five small fixes to today's code:
  - tighten task-file permissions;
  - remove the git token from the VM's git config;
  - audit every read of a stored credential;
  - turn the MCP gateway on by default;
  - send only the selected engine's key.
- **Why now:** the lane is frozen, and these fixes also harden the local runtime.

**Default switches for every phase:** replica VMs on; per-run VMs fully built and tested, but off until someone enables them.

**P8a: the skeleton (medium)**
- **What:**
  - hosts join with certificates and send heartbeats;
  - `boss-hostd` runs VMs from BOSS's desired state and rebuilds itself after a crash;
  - replicas get incoming-only interfaces, and per-run VMs get none;
  - the first guest gateway;
  - the run-plane API.
- **Done when:**
  - a restarted host is back in step within 10 s with no stray VMs;
  - crash, resume and approval tests pass exactly as on the local runtime.

**P8b: agents in VMs, reachable at their VIPs (large)**
- **What:**
  - snapshots, warm pools, signed images and the setup cache;
  - the central gateways, with only placeholders in VMs and tokens held by the host;
  - the jailer;
  - TLS inside VMs;
  - the directors, removing dead VMs in under a second;
  - the subscription test.
- **Done when:**
  - no real secret is found anywhere in a VM, and a coding run still works;
  - per-run VMs have no network interface;
  - the speed targets in section 8.1 are met;
  - an outside client reaches an agent spread across 3 VMs on 2 hosts.

**P8c: high availability (medium)**
- **What:**
  - copies spread across hosts;
  - both directors active;
  - two BOSS API instances;
  - each gateway as a pair;
  - the Fleet doctor.
- **Done when:** every failure in Flow 5 recovers in its stated time, with no duplicated actions.

**P8d: production hardening (medium)**
- **What:**
  - the Postgres standby and backups;
  - rate limits;
  - cloud load-balancer support;
  - retire the old Python sandbox host and the shared static password;
  - an operations runbook.
- **Done when:** a Postgres failover loses no committed work.

**P8e: BGP (medium, for scale and on-prem)**
- **What:** hosts announce VIPs to the routers directly, and the directors retire.
- **Done when:** a dead host is dropped in under 300 ms, and only its own connections reset.

---

## 12. Decisions we need from you

These decisions shape the design most. The low-level design lists all 22. My recommendation is shown for each.

1. **Availability targets** (section 7.1). Confirm 99.95 % reachability, and 99.9 % run start after P8d.
2. **Subscription logins** (section 5.7). Confirm the terms allow automated use across many VMs. Until the one-day test passes, I recommend refusing them on microVMs.
3. **Public or private VIPs.** Which agents must be reachable from the internet, and which only inside your network? This decides the VIP ranges we need.
4. **Agent certificates.** I recommend BOSS's own certificate authority by default, with an optional public certificate per agent for callers who can't trust a private one.
5. **Directors.** I recommend a dedicated pair of small machines in production, sharing the BOSS API machines in small installs.
6. **Strict-secrets mode on by default** for every microVM host. I recommend yes.
7. **Public agents that change things always use per-run VMs**, so outside callers never share a VM. I recommend yes.
8. **Postgres HA:** pg_auto_failover on-prem, and the cloud's managed Multi-AZ in the cloud.
9. **Start P8-pre now?** It is small fixes to today's code. I recommend yes.

---

## 13. Glossary

- **A2A.** Agent-to-Agent, an open protocol for agents to call each other.
- **BGP.** How routers tell each other which addresses they can reach.
- **Checkpoint.** A run's saved progress after each step.
- **DPoP.** A standard that locks a token to a private key, so a copied token alone is useless.
- **ECMP.** Routers spreading traffic for one address across several equal paths.
- **Epoch / fencing.** A number on each run's lease. Writes with an old number are refused.
- **Firecracker.** AWS's open-source microVM engine.
- **IPIP.** Wrapping an IP packet inside another, like a letter in a new envelope.
- **IPVS.** The load balancer built into the Linux kernel.
- **Jailer.** Firecracker's lock-down wrapper.
- **keepalived / VRRP.** Keeps a floating IP on whichever of two machines is alive.
- **Lease.** The right to work on a run, held by one VM at a time.
- **MCP.** Model Context Protocol, the standard way agents call tools.
- **MicroVM.** A tiny, fast virtual machine with its own kernel.
- **On-behalf-of token.** "This agent, acting for this person, with these limited rights."
- **Passport.** BOSS's identity for an agent.
- **Per-run VM.** A throwaway VM for one task, with no network card.
- **Placeholder.** A fake credential in the VM, swapped for the real one outside.
- **Replica VM.** A long-lived copy of an agent that serves callers.
- **Snapshot.** A saved VM, used to start new VMs in milliseconds.
- **Tap.** A virtual network cable between the host and one VM.
- **VIP (virtual IP).** An address that belongs to an agent, not to a machine.
- **vsock.** A private channel between one VM and its host. It is not a network.
- **Warm pool.** VMs restored and waiting for a task.
