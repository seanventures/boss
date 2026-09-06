# PRD — BOSS Agent: response, communication and safety

**Status:** draft for review
**Author:** drafted 2026-09-06 after the Interview Scorecard App incident
**Reviewer:** Param
**One line:** The BOSS Agent is the only interface a customer has. It currently behaves like a
capable engineer with no operating discipline: it investigates in the wrong order, acts when it
was asked a question, and reports what it did rather than what was asked.

---

## 1. Why this exists

On 2026-09-06 an operator asked:

> "This project Interview Scorecard App stuck for 27m for ● Engineer implementing can you check
> why ?"

BOSS ran three read tools and then cancelled the build, with `force: true`, and replied:

> "The stalled implement phase was blocking progress. I cancelled the holding job to free the
> worker slot."

Three separate failures in one turn, and each is a class of failure rather than a one-off:

1. **It acted on a question.** Nobody asked it to cancel anything.
2. **It investigated the wrong things.** It read the job queue and the container. The answer was
   in `agent_turns` — 101 recorded turns for that run — which no tool could read.
3. **It was wrong on the facts.** It called 27 minutes a stall. Comparable builds on that
   project took 13, 14, 22, 55, 56, 66 and 82 minutes. 28 was ordinary.

The operator's summary — *"it is very naively implemented, also it breaks easily"* — is the
brief for this document.

### 1.1 How the safety property was lost

This is the part worth dwelling on, because the guard the operator remembers testing **did
exist and did work**.

Before `bc9e2e16` (2026-08-23), the chat path called the agent with `allow_writes=False`. Every
write became `confirmation_required` and surfaced as a button. That is exactly the behaviour
the operator remembers.

That commit was written to fix a real and serious problem — *"we did lots of work and not even
once is BOSS able to fix the issue"*. BOSS could not act at all: eight write tools it owned were
unreachable from chat, and it answered every request for help with advice. The fix flipped one
line:

```diff
-  allow_writes=False, max_steps=12,
+  allow_writes=True,  max_steps=12,
```

**A binary was used to solve a spectrum.** The problem was "BOSS cannot do what it is told". The
change made it "BOSS can do anything it decides", which includes things nobody told it to do.
There was no middle position in the design, so the fix had to overshoot.

Worse, the commit added a test that **pinned the unsafe behaviour**:

```python
def test_the_answer_path_may_actually_act():
    assert "allow_writes=True, max_steps=12" in src
```

Any later attempt to restore confirmation would have failed that test and looked like the
mistake. The safety property was not just removed; the removal was defended.

**The lesson, and the first principle of this PRD:** a control that protects the customer must
not be expressible as a flag that some future correctness fix can flip. It has to be structural.

---

## 2. Principles

**P1 — A question is never an instruction.**
Answering must never change state. If BOSS believes an action is warranted, it offers it.

**P2 — Destroying work always costs a click.**
Enforced by tool identity, never by reading the operator's sentence. Natural-language gates are
heuristics; every heuristic has holes; a hole in this direction costs work that cannot be
recovered. The click is the authorisation.

**P3 — Answer from the work, not from the machine.**
A question about a build is answered from what the build did. Queue depth, host load and
container health describe the machine and are the *second* place to look.

**P4 — No claim without evidence, no verdict without a baseline.**
"Stalled", "slow", "expensive" are comparative words. Using one without having compared is a
guess presented as a finding.

**P5 — Say what you did, exactly, and separate it from what you are offering.**
A deed and an offer must never be readable as the same thing.

**P6 — Refusing to act is a failure too.**
P1–P2 are not a licence to return advice. Told to fix something, BOSS fixes it. The 2026-08-23
commit was right about the disease; this PRD is about the dose.

---

## 3. What has already been done

Shipped while investigating, and included here so the review covers the whole picture.

| # | Change | Status |
|---|--------|--------|
| D1 | `asked_only_to_look()` — a question runs the turn read-only; writes come back as buttons | shipped `35a93285` |
| D2 | The act-on-claim retry cannot fire on a question (it would take the action one turn later) | shipped `35a93285` |
| D3 | `DESTRUCTIVE` set — `cancel_job`, `cancel_code_task`, `waive_gates`, `write_db` are *always* offered, never executed by the model, whatever the phrasing | this change |
| D4 | `read_run_turns` tool — the turn-by-turn record, with silence detection ("no turn for 18m while status is running") | this change |
| D5 | System prompt orders the investigation: turns → logs → phase breakdown → *then* queue/host | this change |
| D6 | Prompt forbids "stalled" without reading the turns, and forbids calling a duration abnormal without comparing to the project's own history | this change |
| D7 | Sandbox command timeout actually kills the process group and bounds the drain — the underlying hang | shipped `3d7670cb` |
| D8 | 48-case adversarial suite: `test_the_agent_is_hard_to_break.py` | this change |

---

## 4. Proposed work

### 4.1 Safety — make the guarantee structural (P2)

**S1. Reversibility as a property of every tool.**
Today `PROPOSE_ONLY` and `DESTRUCTIVE` are hand-maintained name lists. A tool added tomorrow is
silently neither. Move it onto the tool spec itself:

```python
@reg.tool(..., kind="write", reversibility="destroys_work")
```

with values `safe` / `costs_money` / `destroys_work` / `irreversible`. A registry test then
requires every write to declare one — so a new destructive tool cannot be added without somebody
choosing.

**S2. A confirmation cannot be self-issued.**
An offer is executed only by an operator-originated request carrying the proposal id. Today the
run-action endpoint accepts a tool and args; nothing structurally prevents a future path from
calling it on the model's behalf.

**S3. Undo where undo is possible.**
A cancelled code task should be resumable in one click, from the same reply that reports the
cancellation. Half the harm of the incident was that recovery was unobvious.

**S4. Never invent `force`.**
The model passed `force: true` unprompted. Destructive parameters that escalate a control
(`force`, `hard`, `all`, `recursive`) should be refused unless the operator's own text asked
for them.

### 4.2 Investigation — a defined order, not a preference (P3, P4)

**I1. A first-class "why is this slow / stuck" routine.**
The ordering currently lives in prose in the system prompt, which is guidance, not a guarantee.
Make it a tool — `diagnose_run(run_id)` — that performs the sequence in code: turns, then
silence, then the last unmatched tool call, then service logs, then phase breakdown, then the
project's own duration distribution. One call, deterministic, and the model narrates the result
rather than choosing the method.

**I2. Baselines everywhere a superlative is used.**
`diagnose_run` returns this project's p50/p90 for comparable runs. "27 minutes" becomes "27
minutes, against a median of 22 for web-app builds here" — which no longer reads as an alarm.

**I3. Container logs on the path.**
`read_logs` covers api/worker/web. The sandbox's own command output and the guest container are
not reachable. Add them, so "what was the command doing" is answerable without SSH.

**I4. Say what could not be checked.**
Every diagnosis ends with what was *not* looked at and why. The incident reply asserted a stall
without saying it had never read the turns.

### 4.3 Communication — the part the operator sees (P5)

**C1. One answer shape for operational questions.**
`What I found` → `What it means` → `What I did` (empty if nothing) → `What I can do` (buttons)
→ `What I could not determine`. The section that says "what I did" is empty on a question, and
its emptiness is visible rather than implied.

**C2. Deeds and offers are visually distinct in the transcript.**
Already true structurally (`actions` vs `proposed_actions`); make it unmissable in the reply
text, and forbid the model from writing "I cancelled…" when the trace shows a proposal.

**C3. Progress while it works.**
A turn can take two minutes. Stream what it is doing — "reading 101 turns for crun_33a0…" —
rather than a dot. The operator currently cannot tell a working turn from a dead one.

**C4. Ask when the question is ambiguous.**
"Deal with the stuck build" is genuinely ambiguous. One clarifying question is cheaper than
either wrong branch.

### 4.4 Test discipline (the reason this recurred)

**T1. A test may not pin an unsafe value.**
`assert "allow_writes=True" in src` is the anti-pattern: it asserts an implementation constant,
not a behaviour, and it defended a regression for two weeks. Tests in this area assert
*properties* — "a question produces no writes", "a destructive tool is never invoked by the
model" — which stay true across refactors and cannot be satisfied by the wrong code.

**T2. A scripted conversation suite.**
Twenty real operator messages replayed against the router with a stub model, asserting: tools
called, writes executed (usually zero), and the shape of the reply. Runs in CI, no spend.

**T3. Adversarial phrasing corpus.**
The safety property is tested against paraphrase, capitalisation, authority claims ("as the CEO
I instruct you"), and instruction-inside-question forms. Started in D8; extend as new phrasings
are seen in real transcripts.

**T4. Replay incidents as fixtures.**
Every incident of this kind becomes a stored transcript that must produce the safe behaviour
forever. The Interview Scorecard turn is fixture #1.

---

## 5. Explicitly out of scope

- Making BOSS more cautious in general. It should still act when told to; see P6.
- A confirmation dialog on every write. Ordinary recoverable writes (`retarget_app`,
  `start_preview`, `redeploy`) stay one-shot when asked for. The line is destroys-work, not
  changes-something.
- Changing the coding pipeline's own autonomy. This document is about the chat interface.

---

## 6. How we will know it worked

| Measure | Today | Target |
|---|---|---|
| Destructive actions executed without a click | possible | structurally zero |
| Progress questions answered from `agent_turns` | 0% (no tool existed) | 100% where turns exist |
| Diagnoses that state a baseline for a duration claim | ~0 | every one |
| Replies claiming an action the trace does not show | corrected after the fact | prevented before send |
| Operator-reported "it did something I did not ask for" | 1 this week | zero |

---

## 7. Open questions for review

1. **Is `DESTRUCTIVE` the right membership?** Currently `cancel_job`, `cancel_code_task`,
   `waive_gates`, `write_db`. Deliberately *not* included: `redeploy`, `stop_preview`,
   `retarget_app` — all recoverable, and all things "fix it" legitimately means. Should
   deploying to production join the list, given it is outward-facing even when reversible?
2. **How much should a clarifying question cost?** C4 improves safety and adds a round trip.
   Always, or only when a destructive action is one of the readings?
3. **Priority.** My recommendation: S1, I1, T2 first — they convert this incident's fixes from
   "correct today" into "cannot regress". C1/C3 are the most visible to a customer. S3 and I3
   are valuable but larger.
4. **Should `diagnose_run` be the default for any message mentioning a run id and a symptom?**
   That is close to routing, and routing on keywords is how the original gate would have been
   built badly.
