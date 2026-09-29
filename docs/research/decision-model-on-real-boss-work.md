# The decision model on real BOSS work

*28 September 2026 · run on goldeneye (`boss-dogfood`) against BOSS's own history · Jev key saved through the console*

## The short version

We gave BOSS two real questions it faces every day and let three answerers decide them for 67 real cases from goldeneye's own history:

1. **Jev alone.** TypeSafe's fast decision model, with nothing checking it.
2. **Jev with a second opinion.** Jev answers; when Jev says it is unsure, BOSS asks Claude instead. This is how BOSS is set up now.
3. **Claude alone.** Claude Opus 5.5 answers everything.

What we found:

- **Jev knows when it doesn't know.** In 41 decisions Jev was confident, and it matched Claude in 40 of them. In the other 52 it said it was unsure, and it was often wrong there. This is what makes Jev usable.
- **Jev with a second opinion gave almost the same answers as Claude alone:** 92 of 93 decisions matched. It cost **$0.26 instead of $1.43**, and the confident answers came back in **0.16 seconds instead of about 3 seconds**.
- **Jev alone is not good enough.** It matched Claude on only 63 of 93 decisions. Without the second opinion, don't use it.
- **The biggest win is not the decision model. It is that BOSS makes no decision at these points today.**
  - BOSS spent **$34.38** on automatic "fix the leftover review comments" runs.
  - The decision model says only **one** of those 41 sets of comments needed its own run ($1.43).
  - BOSS also ran **19 brand-new apps at the lightest safety level**. Both models say those needed the full design-and-review treatment.

## First, what is a "decision model"?

Think of an office.

- **A general AI model like Claude is a senior engineer.** Ask it anything and it writes a thoughtful answer. That takes a few seconds and costs money each time.
- **A decision model like Jev is a trained sorter at a conveyor belt.** It can't write anything, and it can't explain itself. It can only put each item in one of the bins you gave it, and say how sure it is. It does that in a fraction of a second, for almost nothing.

BOSS is full of small sorting jobs: *Is this dangerous? Is this worth doing now? Which team gets this?* The idea is to let the sorter handle the easy ones and pass anything it hesitates on to the senior engineer.

This test checks whether that idea holds up on BOSS's own work.

## How we tested it: real work, through BOSS itself

- Nothing here is made up.
  - The cases are real coding jobs and real review comments from goldeneye's history.
  - They cover 110 coding runs across the apps BOSS built in `boss-dogfood`, such as the Feature-Flag Expiry Tracker, the Subscription-Audit Tool and Change Calendar.
- We did not call Jev from a script.
  - We built two ordinary workflows in the console's workflow system, each with a **decision step**, and ran them on goldeneye.
  - Every answer came from BOSS using the Jev key you saved in *Settings → Repo and model*.
  - The key never left BOSS.
- The two workflows are on goldeneye if you want to look:
  - *Decision: how careful should BOSS be with this job*
  - *Decision: is this review feedback worth another paid run*
- Each workflow ran over the real cases three times, once for each answerer above.
- To get the "Claude alone" answers, we switched the project's **Decision model** setting to Claude for one run, then switched it back.

**One honest limit.** For these questions, nobody on the team ever wrote down the "right" answer. So we can't say who was *correct*, only who *agreed*. Claude Opus is the most careful answerer we have, so we use it as the reference. Where the answers differ, the real cases are shown below so you can judge them yourself.

## Real decision 1: "Is this review feedback worth another paid coding run?"

### What happens today

When BOSS finishes a change, its reviewers read the code and list problems.

- Serious problems block the change until they are fixed.
- Minor ones ("nits" and "minors") are allowed through, and the change ships.

Then **BOSS automatically starts a brand-new paid coding run** just to fix those minor comments. It does this for every change, whatever the comments are. Nothing decides whether that run is worth it.

On goldeneye:
- **41 shipped changes** had leftover comments. **Every one of the 80 comments was a "nit" or a "minor".**
- BOSS started follow-up runs for 27 of them, costing **$34.38**. That's more than a quarter of everything spent on coding in this project ($120.29).

A real example: the Feature-Flag Expiry Tracker shipped with one comment:

> *No automated test exercises the new `expiry_target_date` validation path.*

A missing test. BOSS started a whole new coding run for it.

### The question we asked

> A change has already shipped. Its reviewers left these comments, graded below the blocking line. A follow-up run to fix them costs roughly $1–5 and 10–40 minutes. What should BOSS do?
>
> - **Run now:** at least one is a real bug someone would hit.
> - **Batch later:** real but minor; fix it the next time this app is touched.
> - **Skip:** style, speculation, or already handled.

### What came back

| | Run now | Batch later | Skip |
|---|---|---|---|
| **Today (no decision)** | all 27 that got a follow-up | — | — |
| Jev alone | 11 | 25 | 5 |
| Jev + second opinion | **1** | **31** | **9** |
| Claude alone | **1** | **31** | **9** |

- **Jev with a second opinion matched Claude on all 41.**
  - Jev was unsure on 27 of them and handed those to Claude.
  - On the 14 it was sure about, it matched Claude every time.
- **Jev alone was too jumpy.** It said "run now" 11 times where Claude said "run now" once.
  - All 10 of those extras came with confidence under 70%, the line below which BOSS asks Claude. So the second opinion caught every one.
  - Example: on the Subscription-Audit Tool, Jev said "run now" at 35% confidence. The comments were "test files are committed to git" and "the price-rise check only compares first and last charge". Claude called that *"real but minor"*.

**The one that really did need a run** was the Path Verification rebuild. The app is meant to run behind a proxy, but its rate limiter treats every visitor as the same person. Claude put it plainly:

> *Every request will look like it comes from the proxy's IP, and legitimate users will be throttled together as one client.*

That's a real bug real users would hit. Jev on its own also said "run now" here, at 87% confidence.

### What it's worth in money

Of the $34.38 BOSS spent on follow-up runs:

| Decision says | Changes | Follow-up money actually spent |
|---|---|---|
| Run now | 1 | $1.43 |
| Batch later | 31 | $32.63 |
| Skip | 9 | $0.32 |

With this decision in place, BOSS would have started **one** follow-up run instead of 27.

Two caveats:
- "Batch later" work isn't free. It gets done when the app is next changed, so it's postponed, not deleted.
- The bundled fixes should still cost far less than 24 separate runs, each of which starts from nothing.

## Real decision 2: "How careful should BOSS be with this job?"

### What happens today

Every coding job gets a **care level** from 0 to 3:

- **Level 0:** one quick session. Write the code and its tests, done. (~3 minutes)
- **Level 1:** a separate tester, then the coder, then a review.
- **Level 2:** a design phase first (standards, spec, plan, plan review), then a four-reviewer panel. (~40 minutes)
- **Level 3:** everything in level 2, plus **a person must approve the plan before any code is written.** This is for anything touching logins, money, secrets, permissions or deployments.

We looked at 26 distinct real jobs on goldeneye. We found something we didn't expect:

**19 of the 26, all brand-new full-stack apps, ran at level 0.** Neither the keywords nor a model decided that. BOSS's app-building workflow tells the pipeline "no review" for new web apps, and BOSS obeys. So a whole new app with a database, a UI and end-to-end tests, like the Subscription-Audit Tool or the Unified SaaS License-Provisioning Flow, was built in a single quick session with no design phase and no review panel.

### What came back

| | Level 0 | Level 1 | Level 2 | Level 3 |
|---|---|---|---|---|
| **What BOSS actually used** | **19** | 0 | 5 | 0 *(2 not recorded)* |
| The old keyword check | 0 | 1 | 25 | 0 |
| Jev alone | 0 | 14 | 11 | 1 |
| Jev + second opinion | 0 | 1 | 24 | 1 |
| Claude alone | 0 | 1 | 24 | 1 |

- **Jev with a second opinion matched Claude on all 26.**
- **Jev alone struggled here.**
  - It was unsure on 22 of 26 (median confidence 0.42), and on those it often said "level 1" where Claude said "level 2".
  - On the 4 it was sure about, it matched Claude every time.
  - This is a harder question for a sorter: the difference between "a bounded change" and "a new build" needs reading the whole task.
- **Both models agree with each other, and disagree with what BOSS actually did.**
  - Claude's reason, repeated across the new apps: *"It builds a new full-stack app on an empty scaffold, including server-side persistence and end-to-end tests"*, meaning level 2.
  - 22 of the 41 changes with leftover review comments in decision 1 came from runs at level 0. That's suggestive, but this test can't prove the lighter level caused them.
- **One job went to level 3: the US-Equity Signal Screener.** Claude's reason: *"a financial signal product that raises investment-adviser regulatory"* concerns. BOSS ran it with no plan approval. The keyword check said level 1.

### The part that matters most: "Does this touch something sensitive?"

This is the question that decides whether **a person must approve the plan**. So it's the one where a wrong "no" matters.

| | Said "sensitive" |
|---|---|
| Jev alone | 2 (both unsure, both judged "not sensitive" by Claude) |
| Jev + second opinion | 0 |
| Claude alone | 1: **Unified SaaS License-Provisioning Flow** |

**This is the one important miss.** Claude reasoned:

> *A license-provisioning flow most likely grants and assigns user access, seats or entitlements, and may touch billing, so it probably changes access control.*

That's a fair call. Handing out access is exactly what the approval gate exists for.

**Jev said "not sensitive" at 76% confidence.** That's above the 70% line, so BOSS never asked Claude, and the final answer was "not sensitive".

This was the only time in 93 decisions that a confident Jev disagreed with Claude. It happened on the question where a mistake costs the most.

## Speed and cost, side by side

All 93 decisions (67 real cases; the care-level workflow asks two questions per job):

| | Matched Claude | Total cost | Typical time per decision |
|---|---|---|---|
| Jev alone | 63 of 93 | about $0.01 | 0.16 s |
| **Jev + second opinion** | **92 of 93** | **$0.26** | **0.16 s when Jev is sure; ~2.6 s when it asks Claude** |
| Claude alone (Opus 5.5) | — | $1.43 | ~2.6–3.0 s |

- **Jev's own cost is tiny.** Nearly all of the $0.26 is the Claude second opinions.
- **The sensitivity question shows the speed gap best.** Jev was sure on 23 of 26, so the typical answer took 0.17 s against Claude's 3.0 s, and cost $0.015 against $0.71.

## What this means for BOSS

1. **Put a decision where BOSS has none today. That's where the money is.**
   - The follow-up question alone would have saved most of the $34.38.
   - The care-level question would have given 19 new apps a design phase and review panel they didn't get.
   - Both are true whichever model answers. They're BOSS gaps, not Jev features.
2. **Use Jev only with the second opinion.**
   - Jev with a second opinion gives Claude-quality answers at about a fifth of the cost. Most answers are also instant.
   - Jev alone gets a third of these decisions different from Claude.
3. **For safety questions, don't let a confident Jev have the final word on "no".**
   - The license-provisioning miss shows why.
   - The fix: for "does this need a human?" questions, Jev can only say **yes**. A "no" is always confirmed by Claude, or the safety threshold is set so high that it effectively is.
   - This is the same rule we settled on for the keyword check: a model may add a human gate, never remove one.
4. **Fix the new-app level-0 rule.** Separately from any model, whole new apps probably shouldn't skip design and review. That's a one-line policy question for you.

## What this test cannot tell us

- **Who is right.** We measured agreement with Claude Opus, not correctness against a known answer. Where it matters, the real cases above let you judge.
- **The value of the answers in production.** We asked the questions; we didn't let the answers change what BOSS did. The money figures are "what would have happened", worked out from what BOSS actually spent.
- **How Jev does elsewhere.** 67 cases from one project, mostly small web apps. Other kinds of work may behave differently.
- **Where the 70% threshold should sit.** It's the default. At 76%, Jev's one confident miss would have been caught by a threshold of 80%. We haven't tested other values.

## What was built and where it lives

- **Setup (console):** *Settings → Repo and model*.
  - Choose **TypeSafe Jev — workflow decisions only** in the Provider list, paste the key, and press *Verify and save*.
  - The *Who does what* table shows which model makes workflow decisions, in its *Workflow decisions* row.
- **In workflows:** a decision step (`typed_decision`) asks one question with fixed answers. The workflow then branches on the answer.
  - When Claude overrules Jev, the step records both answers.
- **The comparison, re-runnable:** the two workflows on goldeneye. The scripts that start them are in this session's scratchpad. The earlier labelled test set is in `evals/task_rigor/`.
- **Cost of this whole test:** about $3.30 in total over 202 workflow runs, almost all of it Claude (the second opinions and the Claude-only run). Jev itself used a few cents of the $5 credit.
