# Metatron CLI: a governed operator terminal in Rust

Status: design approved in principle (2026-11-17), build not started. Plane module "Metatron CLI", epic BOSSAPP-1367, tickets BOSSAPP-1368 to 1383.
This page is for people. The detailed, agent-facing design is [ARCHITECTURE.md](ARCHITECTURE.md); the feature comparison with OpenAI's Codex CLI is [GAP.md](GAP.md).

## What it is

Metatron is the terminal client of BOSS. Today it is a Python program (`scripts/metatron_tui.py`) that chats with the BOSS agent and calls BOSS's API. It cannot touch the machine it runs on.

The plan is to rewrite it in Rust as a **governed operator CLI**, in the same class as Codex CLI or Kiro: a fast single binary that can run tools on your laptop and on your servers, with you approving what matters.

What an operator would use it for:

- Investigate and debug: ssh to a host, capture `sar` metrics and a packet capture, copy the files back, analyse them with Python the agent writes on the fly, draw a quick dashboard, and attach the evidence to the customer ticket.
- Read a Grafana (or any web) dashboard using an MCP server, or your own logged-in session cookie with your explicit permission for that one site, and explain what happened between two times.
- Use any installed program (ffmpeg, osascript, shortcuts, python, tshark) through one shell tool, plus MCP servers, skills and search.

## What it must not do

**Metatron never writes code.** No file-edit or patch tool exists in it. When a fix needs code, Metatron opens a BOSS code task (or workflow) with the findings attached, and BOSS's governed pipeline builds, tests, reviews and opens the PR.

## Why BOSS is still in the loop

Metatron does the hands-on work. BOSS supplies everything that makes it safe and accountable:

| BOSS provides | What it means for the user |
|---|---|
| Policy | Who may run what, on which host or site. Rules are stored in BOSS and can only tighten what the user sets locally. |
| Credentials | Host keys and service tokens come from the BOSS vault, by reference, short-lived. |
| Audit | Every command, approval and result is recorded with who decided (a rule, the person, or a reviewer). |
| Context | Knowledge graph, past incidents, tickets, skills. |
| Model gateway | The model runs through BOSS, so budgets, routing and receipts apply. There is one agent path for every surface. |

## Picture

```
 your laptop                                              BOSS server
 +-----------------------------------------+            +--------------------------------+
 | metatron (Rust binary)                  |  turn      | one agent turn                 |
 |  terminal UI / `metatron exec`          |----------->| model gateway (budget, route)  |
 |        |                                |<-----------|  policy, vault, audit, memory  |
 |  tool runner  <-- tool_call events -----|  SSE       |                                |
 |   shell (sandboxed)  ssh  transfer      |            |                                |
 |   pcap  python  dashboard  http/MCP     |--result--->|                                |
 |        |                                |            |                                |
 |  approval: read-only | ask | risky-only |--audit---->| audit trail, tickets           |
 |            | act on my behalf           |            |                                |
 |  sandbox + secrets stay local           |--handoff-->| code task: BOSS writes the code|
 +-----------------------------------------+            +--------------------------------+
                  |
                  +--> ssh hosts, MCP servers (Grafana ...), web sites you allow
```

## Approval modes (like Codex)

| Mode | Behaviour |
|---|---|
| read-only | Nothing that changes anything runs. Reads and analysis only. |
| ask | Every action that is not trivially safe asks you first. |
| risky-only | Rules decide. Safe verbs (for example `sar`) run; risky ones ask; forbidden ones never run. |
| act on my behalf | A BOSS-side reviewer approves inside your own permissions, time-boxed and revocable. Hard-prompt classes (packet capture, credentials, writes on hosts, app control) still ask you. |

## Phases and order

Tickets are in Plane; the dependency order is the one that matters.

| Phase | Tickets | What you get |
|---|---|---|
| Foundation | MT-0 (1380) Rust skeleton, CI, notices. MT-10 (1381) BOSS server side | A binary that logs in and streams one agent turn ("hello BOSS"). |
| Feel at home | MT-2 (1369) basics. MT-1 (1368) `sar` on the host allow-list | `login`, `exec --json`, streaming, profiles, completion, `@file`, AGENTS.md, resume. MT-1 is Python-side and can ship any time. |
| Governed power | MT-7 (1375) approval modes. MT-3 (1370) local and remote execution. MT-9 (1378) tools, MCP, skills. MT-4 (1371) evidence and analysis. MT-6 (1374) web and Grafana | The operator scenarios above. |
| Reach | MT-5 (1372) hand-off to BOSS code tasks, MCP client polish, IDE/SDK | Code changes flow to BOSS. |
| Ship | MT-11 (1382) release engineering | Signed binaries; the Python TUI retired. |

Done already: MT-8 (1376) the Codex study, MT-12 (1383) the tool-selection design note. Their content is folded into ARCHITECTURE.md and GAP.md.

## Where the code will live (proposed, please confirm)

A `metatron/` Cargo workspace inside this repository. Reasons: the BOSS server work (MT-10) and the client change together, so one PR can carry both; the installer (`scripts/install/boss.sh`), release process, Apache-2.0 notices (`THIRD-PARTY-NOTICES.md`) and CI already live here; the Python TUI it replaces is here, which makes parity tests easy. The Rust toolchain is isolated by a path-filtered CI job so it does not slow other work. A separate repository is the fallback if the Rust build time or release cadence starts to hurt.

## First milestone: "hello BOSS"

A Rust binary that signs in to a BOSS install (device-code login or a personal API key), starts an agent turn, and prints the streamed reply. Everything else builds on that path.

## Reading order for a new engineer or agent

1. This page. 2. [GAP.md](GAP.md). 3. [ARCHITECTURE.md](ARCHITECTURE.md), in particular sections 5 (BOSS API contract) and 6 (MT-10 draft protocol). 4. Your ticket.
