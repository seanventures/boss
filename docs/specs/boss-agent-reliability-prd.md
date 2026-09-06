# PRD — BOSS Agent: response, communication and safety

**Status:** implemented — awaiting end-to-end validation
**Implemented:** 2026-09-06, deployed to goldeneye (`62ed6a8b`)
**Author:** drafted 2026-09-06 after the Interview Scorecard App incident
**Reviewer:** Param
**Revision:** §4.5 added at the operator's direction — resolution, specificity, non-repetition, and exhausting the evidence before answering.
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

**P7 — Resolve, do not describe.**
The unit of success is the operator's problem being over, not an accurate account of it. An
answer that explains a failure perfectly and leaves the operator to fix it has done a fraction
of the job.

**P8 — Answer the question that was asked.**
Not the topic it belongs to. "Why is this stuck?" is answered by the cause, in the first
sentence. Background is offered underneath, if at all.

**P9 — Never repeat yourself.**
Saying the same thing a second time is a defect, not thoroughness. If BOSS has already told the
operator something in this thread, the next answer builds on it and does not restate it.

**P10 — Exhaust the evidence before answering.**
Tools, the model's own reasoning, both source trees, the database, the logs, the hosts. The
answer comes after looking, not after one lookup.

**P11 — Never give up; hand over a direction.**
"I could not determine it" is an acceptable *finding* only when it names the next specific step
and, where possible, takes it. It is never an acceptable ending.

**P12 — No yes/no questions back to the operator.**
If BOSS needs a decision, it presents the options it has already worked out, with a
recommendation. "Would you like me to investigate further?" makes the operator do the thinking
they came to avoid.

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

### 4.5 Resolution — the agent exists to end the problem (P7–P12)

Added at the operator's direction, and the principles it rests on (P7–P12) are in §2 with the
rest. Safety (§4.1) stops BOSS doing harm; this section is about it being *worth talking to*.
The objective is not a good explanation — it is that the operator's problem is gone.

#### 4.5.1 What the transcripts actually show

Measured over the 79 assistant messages on `boss-dogfood`:

| Measure | Value | Reading |
|---|---|---|
| Answers ending in give-up or defer language | **5 / 79 (6.3%)** | "could not determine", "let me know if", "would you like me to" |
| Answers that called **no tool at all** | **4 / 79 (5%)** | answered from the conversation, not from the system |
| Average tools per answer | **2.86** | against a step budget of 12 — it stops looking early |
| Average reply length | **693 chars** | 5 answers over 1,500 |
| Verbatim repeated openings within a thread | **0** | P9 is currently a risk, not yet a measured defect |

The headline is `2.86`. The step budget was raised to 12 precisely so BOSS could chain a real
investigation — find the run, read its timings, read the code that explains them — and it is
using under a quarter of it. The incident is the same shape: three tools, then a conclusion, on
a question whose answer needed a fourth.

**Repetition is not yet showing in the data**, so P9 is written as a guardrail against a
behaviour the operator has seen rather than one I can currently quantify. If it is happening in
threads I did not measure, T2's scripted suite is where it should be caught.

#### 4.5.2 What to build

**R1. A resolution loop, not an answer loop.**
The turn currently ends when the model produces text. It should end when the problem is
addressed or the operator is asked a real question. Concretely: after drafting, BOSS checks its
own answer against the question — is the specific thing asked actually answered, is there an
action that would end this, is anything asserted that was not read — and continues if not. One
bounded self-check, not an open loop.

**R2. Budget spent on looking, not on prose.**
Enforce a floor as well as a ceiling: an operational question that ran fewer than N tool calls
before answering gets sent back to look further, with the specific gap named. Today the only
bound is the ceiling, and the average sits at 2.86.

**R3. Answer-first formatting.**
The first sentence is the answer. Evidence follows. Background is collapsed or omitted. Today's
average of 693 characters is not the problem — a 693-character answer that opens with three
sentences of context is. This pairs with C1.

**R4. Thread memory: say it once.**
Before sending, BOSS diffs its draft against what it has already said in this thread and strips
what it is repeating, replacing it with a reference ("as above, the hung `npm run verify`").
Cheap, and it is the mechanism P9 needs — a prompt instruction alone will not hold.

**R5. Escalation ladder instead of surrender.**
When the obvious lookup fails, BOSS is required to walk a defined ladder before it may say it
does not know: turn record → service logs → container/host → both source trees → the database
→ the model's own reasoning about what would explain the evidence. The reply then states which
rungs it climbed. "I could not determine it" without that list is not an answer.

**R6. Direction, never a yes/no.**
When BOSS genuinely cannot resolve something, the reply must end with: the single most likely
cause, the one check that would confirm it, and a button that runs that check where one exists.
Never "would you like me to look further?".

**R7. Give the operator the resolution, not just the diagnosis.**
Every diagnosis that has a known fix ends with that fix as an offer — the exact tool and
arguments, one click. The Interview Scorecard reply should have ended with **Retry this build**,
not with a description of a hung command.

**R8. Cross-source synthesis before answering.**
For any question about a running or failed piece of work, BOSS consults, in one pass: the run's
turns, the service logs, the app's code on its PR branch, the platform's own code, the
deployment target, and this project's history for comparison. The reply names what it consulted.
This is the "communicate with tools, models, codebase, infra across all information" requirement
made concrete and checkable.

**R9. Self-service resolution for the operator.**
The operator should be able to end the common problems from the reply itself, without knowing
BOSS's internals: retry a build, resume a cancelled run, rebind a target, restart a preview,
re-run a failed step, roll back a release. Each as a button with the arguments already resolved.
The measure is that an operator never has to ask BOSS *how* to do something they can see is
needed.

#### 4.5.3 New measures

Rolled into the scorecard in §6; repeated here only with the reasoning behind each target.

| Measure | Today | Target |
|---|---|---|
| Answers ending in give-up language with no named next step | 5 / 79 | 0 |
| Answers that called no tool | 4 / 79 | 0 for operational questions |
| Average tools per operational answer | 2.86 | ≥ 6, and no ceiling breach |
| Diagnoses that end with an executable fix | rare | every one that has a known fix |
| Repeated content within a thread | not measured | measured, then 0 |
| Questions BOSS asks that are yes/no | not measured | 0 — options with a recommendation |

#### 4.5.4 The tensions, stated plainly

R1, R2 and R8 all make turns **slower and more expensive**: more tool calls, more tokens, more
wall-clock on a path where the operator is watching a spinner. That is the trade this section
asks for, and it is the right one — but it makes C3 (progress while it works) a dependency
rather than a nicety. An eight-tool investigation behind a silent dot will read as broken even
when it is the best answer BOSS has ever given.

Second tension: P7 ("resolve at any cost") and P2 ("destroying work always costs a click") point
in opposite directions on exactly one class of problem — the ones whose fix is destructive.
The resolution is that P2 wins and P7 is satisfied by the button being *in the answer*, already
filled in. Resolution means the operator is one click from done, not that BOSS presses it.

---

### 4.6 What shipped, item by item

| Item | Shipped as | Note |
|---|---|---|
| S1 reversibility on the tool spec | `ToolSpec.reversibility`, all 25 writes declared | registry test forces the declaration |
| S2 confirmation cannot be self-issued | enforced in `ToolRegistry.invoke` | holds for every caller, not just chat |
| S3 undo | `diagnose_run` returns the retry with real args | offered from the same reply |
| S4 never invent `force` | escalation flags dropped unless asked for | `force`, `hard`, `all`, `recursive` |
| I1 diagnosis routine | `diagnose_run` | one call, fixed order |
| I2 baselines | median/p90 of this project's completed runs | "28 minutes against a median of 22" |
| I3 container logs | **not done** — `read_logs` still covers api/worker/web only | sandbox output is reachable via `read_run_turns` |
| I4 say what was not checked | `checked` / `could_not_check` in every diagnosis | |
| C1 answer shape | prompt: answer first, evidence under | not structurally enforced |
| C2 deeds vs offers | recovery of printed offers in any shape | four shapes seen live |
| C3 progress while working | `agent_progress` + dock polling | "diagnosing the run — crun_33a0…" |
| C4 ask when ambiguous | **partial** — P12 forbids yes/no, no clarify flow yet | |
| R1 resolution loop | one bounded continuation on a thin or dead-end answer | |
| R2 investigation floor | 6 lookups for operational questions | |
| R3 answer-first | prompt only | |
| R4 thread memory | `without_what_was_already_said` | |
| R5 escalation ladder | prompt + `diagnose_run.next` | |
| R6 direction not yes/no | `hands_the_problem_back` triggers a continuation | |
| R7 diagnosis ends in a fix | `diagnose_run.resolution` | |
| R8 cross-source synthesis | prompt + the floor | |
| R9 self-service resolution | retry/cancel offered as buttons | |
| T1 no pinned constants | two such tests rewritten as properties | |
| T2 scripted conversations | `test_scripted_conversations.py`, 30 cases | replays the incident |
| T3 adversarial corpus | `test_the_agent_is_hard_to_break.py`, 48 cases | |
| T4 incident fixtures | the incident is case #1 of T2 | |

**Not done, deliberately:** I3 (container/sandbox logs as a first-class source) and C4's
clarify flow. Both are additive and neither blocks the safety or resolution properties.

---

### 4.7 The follow-up incident, and what it changed (thread `athr_2284c6b8`)

Everything in 4.5 was live, and BOSS still looped. It is worth being precise about how, because
the answer is not "it needed more capability".

Across three turns it gave the same correct diagnosis — port 8000 in use, 8001 free, verified
against the host's own listening sockets — and changed nothing. `inspect_host` (read-only SSH),
`retarget_app` and `redeploy` were all registered, all self-executable, and all in its hands the
whole time. **No tool was missing. No infra access was missing.**

Three faults, in the order they mattered:

1. **The look-only gate refused an instruction.** *"I told you already to redeploy to different
   port, check yourself before applying to the new port... dont keep asking me, you have full
   freedom on the host"* was classified as a question, because the imperative sits after "to" and
   the word "check" appears. R2's own safety gate — added in 4.1 to stop the unasked cancel —
   withheld writes from a sentence handing over the host. **A gate that turns that into a refusal
   is not being careful, it is being deaf.** Fixed with two overrides: handed-over authority
   (`you have full freedom`, `whatever it takes`, `dont keep asking me`) and explicit instruction
   (`I told/asked/want/need you to <verb>`). The question cases the gate exists for are pinned in
   the same test class, so widening it into uselessness fails the suite.
2. **A prescription ended the turn.** "The fix is to retarget the app to port 8001" satisfied
   every existing check — it was substantive, specific, grounded, and not a yes/no. Nothing
   anywhere asked whether the thing the operator wanted had happened.
   `described_a_fix_without_doing_it` now treats prescribe-without-act-or-offer as a fault; a
   proposal button counts as acting, because a button carries the arguments and one click runs it.
3. **Saying it again counted as progress.** No reply was ever compared to the previous one.
   `going_in_circles` scores containment against the last assistant message and forces a
   different approach above 0.45. Measured on the real pair: 0.50 for the rephrased repeat, 0.00
   for a genuinely different answer.

One capability gap was real, and it is small: BOSS reached "8001 is free" by reading raw socket
output and eyeballing it. `find_free_port` answers it properly — a port is free only when nothing
is listening **and** BOSS's registry has not promised it to another app, because a port idle
between deploys is not free and taking it reproduces the collision one app along.

**The lesson for the next round:** the failure was not in what BOSS could do. It was that three
separate mechanisms each accepted "described the fix accurately" as a finished turn. The property
worth defending is *did the operator's problem change state*, and every check added here is a
proxy for it.

Separately, the sentence that started the thread — "the capability reported failure without a
reason" — is gone. It is the absence of an error message dressed as one: it names nothing to look
at and implies the system knows what happened when it does not. A failing capability now reports
whatever it returned, however unplanned the keys; where it returned nothing, it says so, says the
defect is the capability's rather than the app's, and names the two things to read next. The
step's `output` also survives the failure, which it did not before — the stored value was `{}`,
so BOSS could not explain the failure to the operator either.

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
| Answers ending in give-up language with no named next step | 5 / 79 (6.3%) | 0 |
| Answers that called no tool at all | 4 / 79 (5%) | 0 for operational questions |
| Average tools per operational answer | 2.86 of a 12 budget | ≥ 6 |
| Diagnoses that end with an executable fix | rare | every one with a known fix |
| Repeated content within a thread | not measured | measured, then 0 |
| Questions BOSS asks that are yes/no | not measured | 0 — options with a recommendation |

§4.5.3 carries the same numbers with their derivation; this table is the one to review against.

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
5. **What is the floor for R2?** Six tool calls on an operational question is my proposal, from
   the observed 2.86 average and a budget of 12. Too high and cheap questions get expensive;
   too low and it changes nothing.
6. **Does §4.5 change the cost profile more than you want?** R1/R2/R8 mean most operational
   turns cost several times what they do today. If that is unacceptable, the honest lever is to
   apply the floor only to questions about running or failed work, and leave everything else at
   today's behaviour.
7. **Is C3 (streaming progress) a blocker for §4.5 rather than a follow-on?** My view is yes —
   see §4.5.4.
