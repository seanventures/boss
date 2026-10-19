# Where BOSS should use Jev: a backlog for typed decisions

Status: recommendation, 2026-10-16. Each section below is one Plane ticket. Paths are relative
to `engine/boss_engine/services/` unless they say otherwise.

## What Jev is for in BOSS

`typed_decisions.py` asks a model a question whose answers are fixed in advance. The model picks
one option label and gives a confidence. Anything outside the options is refused, not guessed.
`SecondOpinionDecider` asks Jev first and re-asks Claude only for the answers Jev was unsure of.

Jev is the right tool when **all four** of these are true:

1. Code is judging what **natural-language text means** (a brief, a chat message, a reply, an
   error) and not just matching a code, an enum or a number.
2. The answer is **one of a small fixed set** (yes/no, a tier, a class, a route).
3. The keyword list or regex doing it today **gets real inputs wrong**. Every ticket below names
   an input that it gets wrong today.
4. A wrong answer can be **made safe**. Either it is reversible, or Jev is only allowed to push
   toward the cautious answer.

## Where it is used today

| Use | Where | Live? |
|---|---|---|
| Workflow `typed_decision` step | `typed_decisions.run_step`, `workflow_capability_runner.py:2400` | Live on the governed path (`TypedDecisionSpend`) when a key is held. No shipped template uses it. |
| Model-router tier classifier (also asks the rigor questions) | `model_router/classifier.py:311`, `:381` | Held by default; `BOSS_ROUTER_JEV_LIVE=true` lifts it onto the governed path (`store.GovernedJevDecider`, BOSSAPP-579). Held, it shows *Jev · paused* and uses the rules. |
| Rigor/sensitivity questions | `pipeline_profiles.model_risk_tier` | **Written but not wired in.** The build pipeline still calls the keyword `infer_risk_tier` (`governed_code_task_service.py:3558`). |
| Key proof in setup | `setup_service.py:1013` | Live on the governed path (`store_decision_key`, no fallback). |

## Rules for every ticket

- **Monotonic only where it matters.** Wherever the answer gates spend, writes, approvals or a
  safety check, combine Jev with the existing rule so that Jev can only make the outcome
  **more cautious**: `refuse = regex OR jev`, never `refuse = jev`. In that direction a bad
  answer, a prompt injection or an outage costs at most one extra question to a person.
- **The keyword rule stays as the fallback.** Use it when Jev is held, has no key, times out or
  gives a low-confidence answer. This is already how the router works.
- **Untrusted text is advisory.** Repo content, logs, alerts, issue bodies, emails and web
  research may only feed a Jev answer that labels, skips or escalates, never one that acts.
  TypeSafe's own docs list prompt injection as a weakness.
- **Record both opinions.** Log the keyword answer and the Jev answer on every call (the
  `first_opinion` pattern). Then "how often does Jev disagree, and who was right" can be
  answered from data.
- **Batch the questions.** Put all the questions about one piece of context into one `decide`
  call. Context is what costs money.

---

## P0: groundwork (without these, nothing below is live)

### JEV-0a: Lift the BOSS-451 hold for governed typed decisions (BOSSAPP-579)
**Why:** Every live Jev call was paused (`typed_decisions.GOVERNANCE_HOLD`). The spend path is
built in `typed_decision_spend.py` (reservation, lease, receipt, settlement); the workflow step
and the key proof already ran on it. The router's Jev classifier was the one caller that would
have used a bare client, so it stayed held.
**Shipped:** `TypedDecisionBatchSpend` carries several questions about one context through the
same envelope, `store.GovernedJevDecider` puts the router's call on it, and the hold lifts for
the router behind `BOSS_ROUTER_JEV_LIVE` (off by default). One test per condition in
`engine/tests/test_a_router_jev_call_is_governed.py`.
**Still to do:** set the switch on an install that holds a key and run the agent battle-test set
(BOSSAPP-578) against it; the UI then stops showing *Jev · paused* there.

### JEV-0b: Check in the decision eval (`evals/task_rigor`) and add Jev to it
**Why:** `pipeline_profiles.py:317` and `config.py:135` cite an eval saying that keyword lists
missed 14 of 22 sensitive tasks against Opus's 0. That eval is not in the repo or in its git
history, so the claim cannot be re-run, and Jev has never been measured. We should not switch
any gate to Jev without a number.
**Build:** a labelled corpus for each decision below (start with rigor, sensitivity and
look-vs-act), plus a runner that scores the keyword rule, Jev, Jev plus the Claude second
opinion, and Claude on its own. Report accuracy, false negatives on the cautious class, p95
latency and cost per call.
**Done when:** `make eval-decisions` (or a similar command) prints that table, and CI runs the
keyword-only part with no network.

### JEV-0c: Let workflow authors and the drafter create `typed_decision` branches
**Why:** No shipped template uses the step. `workflow_drafting.py` only produces linear
sequences, so a drafted "if the ticket is a bug, do A, otherwise B" becomes a person gate or a
free-text agent step.
**Build:** teach `_draft_prompt` and `_spec_from_steps` (`workflow_drafting.py:23-120`,
`:624-648`) a typed-decision step followed by a `condition`. Add one sample to
`workflow_samples.py`.

---

## P1: high impact (a safety gate or real spend is decided by keywords today)

### JEV-1: Use `model_risk_tier` in the build pipeline
**Where:** `governed_code_task_service.py:3558` calls `infer_risk_tier`, the keyword version.
The keyword lists are in `pipeline_profiles.py`: `SENSITIVE_MARKERS` at `:16`, and
`_is_trivial_change` / `_is_incremental_change` at `:120-129`.
**Problems today:**
- The sensitivity check matches whole words, so the marker "auth" never matches
  "authentication".
  - Inputs that come back **not sensitive**: "Add authentication to the API", "Store user
    passwords hashed", "Rotate API tokens and secrets", "Add OAuth sign-in with Google", "Add
    Stripe checkout", "Run the database migrations". These run at tier 2 with no human plan
    sign-off.
  - "Restyle the login button colour" comes back as tier 3, a false alarm.
- The tier markers are plain substrings:
  - "css" matches "access", "api" matches "rapid", and "state" vetoes the trivial lane.
  - "Adjust the existing theme: drop the CSRF middleware" can reach **tier 0**.
**Why Jev:** this is exactly the "colour of the login button versus how login works" problem
that `typed_decisions.py` was written for. The questions (`_rigor_questions`) already exist, as
do the tests (`test_a_typed_decision_never_guesses.py`).
**Change to make while wiring it in:** today `model_risk_tier` *replaces* the keyword answer to
the sensitivity question. Make it `sensitive = keyword OR jev`, so a model "no" can never
remove the plan gate. Jev may still lower the *size* tier (0, 1 or 2), because the suite-intact
gates hold at every tier.
**Value:** the right review depth for every build. Fewer 39-minute pipelines for CSS tweaks,
and no unreviewed auth changes.

### JEV-2: Add a Jev veto to the auto-approve backstop in full-auto mode
**Where:** `autonomy.py:56` `IRREVERSIBLE`, checked at `:151`.
**Problem today:** a regex over the gate's question text decides whether a gate can approve
itself. None of the following match it, so each auto-approves when its tier is 1 or lower and
its cost is under the cap:
- "Overwrite the live database with the staging snapshot"
- "Force-push main"
- "Truncate the orders table"
- "Make the S3 bucket public"
- "Invite the contractor to the org"
**Why Jev:** one yes/no question: "can this act be taken back?" The regex only knows
vocabulary. Jev judges the act.
**Design:** refuse when the regex matches **or** Jev says "irreversible" **or** Jev's confidence
is low. Jev is never asked to *clear* a regex hit. In this direction prompt injection is harmless:
the worst case is that a person is asked.
**Value:** closes a real "approved by nobody" hole in full-auto mode.

### JEV-3: Look versus act in Ask BOSS (the write gate)
**Where:** `intent_router.py:1113` `asked_only_to_look`, which sets
`allow_writes=not _looking_only` (`:2339`). The regexes it uses are `_ASKED_TO_ACT` (`:582`),
`_AUTHORISED` (`:1063`) and `_INSTRUCTED_TO` (`:1073`).
**Problem today:** this gate exists because of the 27-minute cancelled build. These inputs still
enable writes:
- "Why is it stuck? Stop, do not touch anything, just tell me" (the leading "Stop" reads as an
  order)
- "As I said, it is slow. What is going on?"
- "I told you it was slow yesterday too, why?"
**Design:** asymmetric. Jev may turn an "act" into "look only", which makes writes become
confirmation buttons. Jev may never turn writes on. Low confidence means look only.
**Value:** stops the agent from cancelling or redeploying things in answer to a question.

### JEV-4: Shepherd failure classification and the code-verdict-or-platform-kill split
**Where:** `task_shepherd.py:154` `classify` (signatures at `:92-152`) and
`shepherd_watch.py:488` `is_code_verdict` (markers at `:436`).
**Problems today:** first-match substrings over error text.
- Any error that contains `405`, such as `src/app.ts:405`, is classed as STALE_BASE and gets
  rebased.
- "conflicting peer dependency" is classed as MERGE_CONFLICT.
- "pytest: 3 errors during collection" has no marker, so it is treated as a platform kill and
  retried blind (a full pipeline spend).
**Why Jev:** the outcomes are already a fixed enum (about 19 classes), and the result already
drives remedies (the autonomous retry classes and `redeploy_deployment`).
**Design:**
- `recorded_class` still wins. Jev only replaces the string fallback.
- Jev answers may pick a remedy only among the **non-destructive** classes. For DEPLOY / CANARY
  / HEALTH remedies, require that the keyword rule and Jev agree, or ask a person.
- The error text can contain test output, so a low-confidence answer means UNKNOWN, which
  escalates.
**Value:** fewer wasted blind retries, and rebases and redeploys that fit the actual failure.

### JEV-5: Chat intent, meaning which governed work a message starts
**Where:** `intent_router.py:3340-3465` `_deterministic_intent`, and
`metatron_captain.py:228-271` `interpret_turn`, whose `act` branch starts an auto drive.
**Problems today:**
- "I'd like to compare notes on last week's deploy" starts `run_discovery`, which reserves
  budget.
- "First the deploy failed, then the preview hung" becomes a multi-phase plan.
- In the Captain, "Why did the build fail" (with no "?") is classed as `act`, which calls
  `drive(auto=True)`.
**Why Jev:** this is a typed choice between about 8 intents (about 14 for the Captain), and the
safe default (just answer, or `status`) already exists.
**Design:** low confidence falls back to answer-only or `status`. Spend still goes through the
existing budget reservation.
**Value:** money is no longer spent on work that nobody asked for.

### JEV-6: Fix the approval parsing of decision replies by email and Slack, with Jev as a veto
**Where:** `comms.py:1381` `_parse_inbound_message`, consumed by `decisions.transition`
(`apps/api/app/main.py:22457-22520`).
**Problem today:** the first of approve / reject / revise / execute / delegate found **anywhere**
in the subject, the body or the quoted text wins. "I won't approve this. Reject dec_ab12."
approves it. This is a bug whether or not Jev is used.
**Design:**
1. Without Jev: parse only the first line of the new text (strip the quoted reply), and if
   more than one action word appears, send the reply to the deadletter queue.
2. With Jev: if Jev's reading disagrees with the parser, or is not confident, deadletter the
   reply and ask for a clearer answer. Jev **never** approves on its own. The text is external,
   so this rule is non-negotiable.
**Value:** an email reply can no longer approve an irreversible action that the person meant to
reject.

---

## P2: medium impact (cost, scope or quality, all reversible)

### JEV-7: Build scope: ops endpoints, e2e tests, bundler and Postgres
**Where:** `build_scope.py:49-57`, and `build_services.py:147` (`_PG_WORDS`).
**Problem:** whole-word regexes with no handling of negation. These all add work nobody asked
for: "we will **not** deploy this, it's a local prototype" adds Prometheus `/metrics`, "no
metrics needed" adds metrics, "not a React app" adds vite, and "we will not use Postgres"
provisions Postgres.
**Why Jev:** four yes/no questions on one brief, in one call. The structural signals
(`workflow_deploys`, the manifest) stay deterministic and still win.
**Value:** stops adding ops and test scope that the brief did not ask for (the 35-minute
"Notes Lite" case in the docstring).

### JEV-8: Refusing an unsupported stack: "build in" versus "integrate with"
**Where:** `stacks.py:707-832` (`unsupported_named`, `check_text`, `check_architecture`).
**Problem:** "Build a Python client for our existing Go service API" and "Python app that parses
`Cargo.toml` files" are both refused before they start.
**Why Jev:** one question: "does the brief ask BOSS to *write code in* X, or only to *talk to*
X?" Keep the parse of the `Stack:` line deterministic.
**Value:** fewer false refusals of valid work.

### JEV-9: Choosing the web-app scaffold in PRD decomposition
**Where:** `prd_decomposition.py:30-44`, used at `:157`. A "yes" commits a full-stack scaffold to
the work branch.
**Problem:** "a REST API with no frontend" and "a CLI that prints a dashboard summary" both get a
React scaffold. "A browser tool for invoices" does not.
**Options:** `fullstack` / `frontend_only` / `not_web`.

### JEV-10: Work-class routing from a directive
**Where:** `workflows.py:7516` `CODE_TASK_WORK_CLASSES = ("project.new","any")` and
`tools/builtin.py:1270`.
**Problem:** the `fast_fix`, `minor_feature` and `major_feature` workflows exist but can never be
picked from a directive. Everything runs as a new project.
**Design:** a typed choice over `WORK_CLASSES`. Low confidence means `project.new`, which is
today's behaviour. Deployment stays manual behind the risk floor.
**Value:** a bug fix runs the bug-fix workflow instead of the full new-project one.

### JEV-11: Detecting the UI axis (`is_ui_bearing`)
**Where:** `design_prompts.py:35`, called at `governed_code_task_service.py:3553`.
**Problem:** substring checks. "Design the database schema for the billing component" counts as
UI, and "css" matches "access" and "process".
**Bundle with JEV-1:** add it as one more question in the same `decide` call. It costs almost
nothing extra.

### JEV-12: Classifying operator feedback (scoped tweak versus full rerun)
**Where:** `feedback_agent.py:16-19`, `:58-62`, `:136-140`.
**Problem:** "Typo on the authentication page header" triggers a full governed rerun. "Checkout
total wrong" has fewer than 6 words, so it is asked to clarify. "add" as a substring matches
"address".
**Options:** `scoped_tweak` / `full_rerun` / `too_vague`. The user still confirms.

### JEV-13: Follow-up messages while a plan is pending
**Where:** `intent_router.py:1585-1616`.
**Problem:** "Don't cancel it, just move step 2 earlier" and "Add a step to cancel stale jobs"
both **dismiss** the plan.
**Options:** `dismiss` / `discuss` / `revise` / `approve`. Low confidence means `discuss`.

### JEV-14: The orchestrator's phase for a plan task
**Where:** `orchestrator_next_move.py:33-56`, used at `:300`.
**Problem:** "Implement login and add unit tests" is classed as `qa`. "Build the deploy settings
page" is classed as `release`, so the implement path is skipped.
**Options:** `qa` / `release` / `architecture` / `scope` / `implement`.

### JEV-15: Branches in shipped workflow templates (the first real uses of the step)
- **Incremental Build** (`workflow_catalogue.py:252-256`): the branch is a substring check,
  `contains "## Increment k"`. "### Increment 2" or "## Increment 2 (deferred)" counts as
  planned. "## Phase 2" does not. Make it a typed decision: "is increment k planned for this
  release?" This is the cleanest first use of the step.
- **trend-to-content** and **sales-signal** (`examples/*/setup.py`): the assess step writes its
  verdict as free text, then the draft is written anyway. A typed decision (`ours` / `not_ours`
  / `no_angle` and `pursue` / `skip` / `insufficient_evidence`) lets the run **skip** the
  drafting step. The input is web research, so the branch may only skip, never send.
- **Research-Led Discovery**: a typed recommendation (`build` / `don't build` /
  `research more`) to pre-fill the `spec_review` card. A person still decides.

### JEV-16: Plan-review verdict and reviewer substance
**Where:** `governed_code_task_service.py:5164` `plan_ok = "REVISE" not in summary.upper()`.
The code's own comment at `:3576` says this is false on every measured run. Also
`_refutation_is_substantiated` (`:2726`).
**Design:** "Is the plan sound, or does it need revision?" and "Does this refutation name a
concrete defect?" Both are advisory. A human still approves the plan, and the strict profiles
fail closed on a low-confidence answer.

---

## P3: low impact or display only (do these when convenient, or batch them)

| Ticket | Where | What goes wrong today |
|---|---|---|
| JEV-17: Deadletter class and model-capacity detection | `deadletters.py:135`, `:170` | The regex runs over the whole JSON payload, so a `.git` URL makes every row "git", and "credit card form test failed" raises a model-capacity Decision. |
| JEV-18: Pasted-failure detection in Ask BOSS | `reported_failure.py:57` | "Why did the release notes generator error out?" is read as a pasted failure. "HelpDesk won't come up after I pushed" is missed. |
| JEV-19: Routing an SRE question to a connector and query | `sre_ask.py:13-110` | "spl" matches "display", so the question goes to Splunk. "pages load slowly" goes to PagerDuty. |
| JEV-20: Categories for Ask Metatron | `metatron.py:121-155` | "cap" matches "capability", so the question is answered from the budget. |
| JEV-21: Model setup failure pill | `setup_checks.py:312` | "insufficient permissions" and "load balancer" are shown as `no_credit`. Keep the status-code branches. |
| JEV-22: Mapping external tracker status to board columns | `release_planning.py:203` | "In Review" and "Won't Do" land in "planned". Classify each status once and cache the result. |
| JEV-23: Detecting the reply-quality rewrite | `intent_router.py` `_no_action_was_claimed`, `described_a_fix_without_doing_it`, `hands_the_problem_back` | "The previous engineer has fixed the port" triggers a false "nothing changed" correction. Batch these as one call per reply. |
| JEV-24: Deploy failure headline | `infra_view.py:1995` | "docker pull timed out" is headlined "Docker problem". |

Where the input is untrusted (app logs, alert text, issue bodies), the Jev answer is a label or a
hypothesis ranking only:
- `investigation.py:370` `_corroborates`: a cause key "db" matches "feedback". It may only lower
  confidence, or advise.
- `github_triage_agent.py:146`: the issue body is attacker-written. Fine only because every
  verdict already waits on a maintainer.
- `iac_verify.py:312`: a credential-only plan failure. Jev may only keep the step **failing**,
  never turn it into a pass.

---

## Where we should NOT use Jev

Keep these deterministic. A model here adds cost, latency or attack surface and buys nothing:

- **Secret redaction and scrubbing:** `redaction.py`, `share_scrub.py`, `identity/redaction.py`,
  `templates/secret_scan.py`, `knowledge_context.py`, `action_preview.py`.
- **Security deny-lists:** the SQL `_MUTATING_KEYWORDS` in `agent_loop.py`, `grants_something`
  in `agent_persona.py`, and the `eval(` / hard-coded key lint in `github_pr_review_agent.py`.
- **Strict output-contract parsers:** `_parse_security`, `_parse_consistency` and `_parse_arbiter`
  in the governed service, and the `_FINDING_RE` in `review_comments.py`. Fix the loose ones
  (`verifier_debate.py` `"verdict: survived" in out`) with a last-line parser, not a model,
  because the text being judged is adversarial.
- **Exact codes, enums and numbers:** HTTP and git status codes (`transient_retry` HTTP
  helpers), meeting vote tokens (`meeting_consensus.py`), budget, admission, value scoring,
  anomaly thresholds, and rollout health enums.
- **Anything where a person's explicit answer is the point:** MCP confirmation digests, the
  triage of security findings, and the approval gates for compliance and data migration.
- **High-volume display tagging:** per-line log levels (`preview_log_ingest.py`, `agent_turns.py`).
  A model call per line is not worth it.

## Suggested order

1. **JEV-0a, JEV-0b**: without them nothing is live, and without them we can't measure.
2. **JEV-1, together with JEV-11**: the questions and tests already exist, and wiring them in
   gives the largest gain in cost and safety.
3. **JEV-2, JEV-3, JEV-6**: closes safety holes. Fix JEV-6's parser bug now, even before Jev.
4. **JEV-4, JEV-5**: spend.
5. **JEV-0c, then JEV-15**: the first real use of the workflow step in a template.
6. P2 and P3 as capacity allows. Each one is a small question added to an existing call site.
