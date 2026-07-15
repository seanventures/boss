# GitHub Community Agent

> **Summary.** Turn GitHub itself into BOSS's community interaction surface: an AI agent that lives wherever the OSS is hosted, listens for issues/comments/PRs via a signed inbound webhook, **triages** feature requests (accept / decline / needs-info) and — on accept — fans them into real work via the existing decomposer, and **reviews** contributor pull requests (inline code comments + a value/ROI recommendation to the branch owner), persisting every verdict as an auditable, human-approvable `decision`.
> · **User ask:** 3 (an in-GitHub agent any developer/user/customer can talk to for feature requests; it triages and decides, and does PR code-review on developer submissions, working with the branch owner to recommend whether a feature adds value)
> · **Module:** GitHub Community Agent
> · **Priority:** P1-oss-launch

---

## 1. Context & Goal

The open-source strategy is "open the engine, keep the control plane paid." For that to work, the **place where the community shows up is GitHub** — the repo where the engine lives. A drive-by contributor opens an issue asking for a feature; a downstream user comments "can BOSS also do X?"; someone forks and sends a PR. Today none of that touches BOSS. A human maintainer has to read it, decide, and either hand-triage it or manually create a code task.

**Goal:** make BOSS the first responder in its own repo. Specifically:

1. **A community interaction surface in GitHub.** Any developer/user/customer files an issue or comments; the agent answers, asks clarifying questions, and reaches a decision — no separate portal, no login, meet people where OSS collaboration already happens.
2. **Triage that decides and acts.** New feature-request issues/comments are classified and given an accept / decline / needs-info verdict. On **accept**, the request is turned into a concrete plan via the existing intake decomposer (`/v1/commands/propose`), so a community ask becomes real, dependency-sequenced work — not a backlog note.
3. **AI PR review for contributor submissions.** When an outside developer opens a PR, the agent runs the code agent / LLM against the diff, posts **inline** review comments, and gives the **branch owner** a value/ROI recommendation — does this feature actually add value, is it worth merging. The recommendation feeds the **human gate**; BOSS never auto-merges (merge stays off by default).
4. **Auditable and human-approvable.** Every triage verdict and every PR-review recommendation is persisted through the existing `decisions` service, so a maintainer can see, override, or approve each one.

This is **THE** community engagement front door for the OSS project. It is deliberately built on shared primitives (the spec-00 inbound webhook receiver, the spec-07 intent router + decomposer, the `decisions` service) rather than a bespoke bot, so the same governance and audit trail that covers internal work covers community-originated work.

---

## 2. Current State (ground truth)

BOSS's GitHub integration is **outbound-only**. It can author changes but cannot hear anything back.

- **`apps/api/app/services/github_adapter.py` — write-side only.** `create_pull_request` (`github_adapter.py:183`), `merge_pull_request` (`:223`, and merge is off by default), `upsert_file` / `upsert_binary_file` (`:148` / `:154`), `ensure_branch` (`:133`), `create_deployment` (`:240`). The only "read from GitHub" is `wait_for_commit_checks` (`:319`) which polls **CI check-runs** to gate a merge (`_evaluate_check_runs:362`). All requests go through `_request` (`:98`) with `_headers` (`:91`) using a Bearer token. **There is no `create_issue_comment`, no `create_review`, no issue/PR listing for interaction.** "PR validation" today means external CI status, not code review.
- **`apps/api/app/services/github_app_adapter.py` — identity only.** A GitHub App that mints a short-lived, repo-scoped **installation token** (`installation_token:94`, `token_for_repo:143`, `contents+PR write` scope). Configured by `github_app_id` (`config.py:455`), private key (`:457`/`:460`), installation id (`:462`). It exists purely to hand a ~1h token to the sandbox (clone) and the adapter (PR authoring). It does **not** receive anything.
- **No inbound webhook receiver anywhere.** `grep` for `X-Hub-Signature` / `webhook_secret` / HMAC over the API = **zero hits**. The one inbound message path, `POST /v1/comms/inbox/ingest` (`main.py:7871`), is a regex parser (`comms.py:_parse_inbound_message:1254`) for decision-replies and messages — no signature verification and no free-form intent handling.
- **Events are a poll-only ledger.** `emit_event()` (`services/core.py:39`, ~343 call sites) writes the append-only `events` table; the only surface is `GET /v1/events` (`main.py:9052`). There is **no pub/sub, no subscriber, no webhook-in dispatcher** — this is the load-bearing gap that spec 00 fills.
- **Intent→many-tasks already exists** and is reusable. `CommandIntakeService.propose_command` (`intake.py:220`) turns an NL command into an LLM plan of phases; `dispatch_plan` (`intake.py:116`) fans out one dependency-sequenced child task per phase. Exposed at `POST /v1/commands/propose` (`main.py:4924`) and `POST /v1/plans/{id}/dispatch` (`main.py:5195`).
- **The decisions service already gives us the audit + human-gate rail.** `create_blocking_decision(connection, project_id, task_id, question, recommendation, cost_summary, reason, *, risk_tier, default_action)` (`core.py:96`) writes a `decisions` row (24h deadline, safe default) and `DecisionService` (`decisions.py:28`) manages lifecycle (`expire_overdue:39`, digests). This is exactly the human-approvable persistence the user asked for.

**Net:** BOSS can open PRs and read CI, but it is deaf to issues, comments, PR conversations, and reviews. Everything in this spec is net-new inbound behavior layered on top of the existing App-token outbound path.

---

## 3. Target Design & Approach

```
GitHub (issues / issue_comment / pull_request / pull_request_review)
        │  signed POST (X-Hub-Signature-256)
        ▼
[spec 00] Inbound webhook receiver ──HMAC verify(webhook_secret)──► normalize ──► emit_event("github.<kind>")
        │                                                                              │
        │ (deliverable is durably recorded before any agent runs)                      ▼
        ▼                                                                        events ledger
[spec 07] Intent router  ─── dispatches by event kind + payload ───┐
        │                                                          │
        ▼                                                          ▼
  TRIAGE agent                                              PR-REVIEW agent
  (issues / issue_comment)                                 (pull_request / pull_request_review)
        │                                                          │
  classify feature request                                 run code_agent/LLM over the diff
  → accept / decline / needs-info                          → inline comments (create_review)
        │                                                  → value/ROI recommendation
  accept → POST /v1/commands/propose                              │
  (decomposer, spec 07)                                           │
        │                                                          │
        └──────────► create_issue_comment (post verdict back) ◄────┘
        │                                                          │
        ▼                                                          ▼
   decisions.create_blocking_decision(...)  ◄─── every verdict/recommendation (auditable, human-approvable)
                                                     │
                                                     ▼  merge stays OFF by default
                                             human gate (maintainer approves/overrides; merge is a human action)
```

### 3.1 Inbound webhook endpoint (built on spec 00)

Spec 00 provides the generic signed inbound receiver and the event-dispatch fan-out. This spec registers GitHub as a **source** on it:

- **New endpoint `POST /v1/webhooks/github`** (auth-exempt like other machine ingress, but protected by signature). It reads the raw request body, computes `HMAC-SHA256(webhook_secret, raw_body)`, and constant-time-compares against the `X-Hub-Signature-256` header. Reject with 401 on mismatch or missing signature. **Never** parse the body before the signature check.
- **New setting `github_webhook_secret`** in `config.py` (`Field(default=None, alias="BOSS_GITHUB_WEBHOOK_SECRET")`), placed next to the existing GitHub App fields (`config.py:455-462`). Inert (endpoint returns 503 "webhook not configured") when unset, mirroring how `github_app_adapter` is inert without an app id.
- **Handled event types** (from the `X-GitHub-Event` header): `issues` (opened / edited / reopened), `issue_comment` (created), `pull_request` (opened / synchronize / reopened), `pull_request_review` (submitted). Everything else is acknowledged 204 and dropped.
- **Loop-guard:** ignore any delivery whose `sender.login` is BOSS's own App (so the agent never triages its own comments). Also de-dupe on `X-GitHub-Delivery` id to make redelivery idempotent (GitHub retries).
- On accepted deliveries, normalize to a compact envelope and `emit_event(connection, project_id, "github.issue_opened" | "github.issue_comment" | "github.pr_opened" | "github.pr_review", actor="github", entity_type, entity_id, payload)`. The event write is the durability boundary — agents run **off** the event, not inline in the request handler, so a slow LLM never blocks GitHub's delivery timeout and redelivery is safe.

### 3.2 New outbound adapter methods (reuse the App installation token)

Add to `github_adapter.py`, using the existing `_request` (`:98`) / `_headers` (`:91`) path and the same `_auth_token` (`:73`) that already prefers the App installation token:

- **`create_issue_comment(self, issue_number: int, body: str) -> dict`** → `POST /repos/{repo}/issues/{n}/comments`. Used for both issue triage replies and top-level PR comments (PRs are issues for the comment API).
- **`create_review(self, pull_number: int, body: str, event: str, comments: list[dict]) -> dict`** → `POST /repos/{repo}/pulls/{n}/reviews` with `event` in `COMMENT` | `REQUEST_CHANGES` | `APPROVE` and `comments=[{path, line, side, body}, ...]` for **inline** annotations. Default `event="COMMENT"` — the agent comments and recommends, it does not gate the PR by approving/blocking (the human gate does).
- **`get_pull_request_diff(self, pull_number: int) -> str`** → `GET /repos/{repo}/pulls/{n}` with `Accept: application/vnd.github.v3.diff`, so the PR-review agent has the unified diff to reason over. (Read-only companion to the write methods.)

The App installation token already carries `contents + pull_requests` write; posting comments/reviews needs the **`issues: write`** and **`pull_requests: write`** permissions on the installation — an App-manifest permission bump, not new code (see §6).

### 3.3 TRIAGE agent (issues + issue comments)

Triggered by `github.issue_opened` / `github.issue_comment` events. Shares the **spec-07 intent router** so the same "what does this person want" classification serves both the orchestration agent and the community agent.

1. **Classify.** LLM over the issue title/body (+ recent comment thread) → `{intent: feature_request | bug | question | spam/off-topic, feature_summary, missing_info[]}`. Non-feature intents get a helpful canned reply and stop (bugs may still be logged as events for a maintainer).
2. **Decide** accept / decline / needs-info:
   - **needs-info** when `missing_info` is non-empty → post a comment asking the specific clarifying questions; no decomposition yet. The next `issue_comment` re-enters triage with the answers.
   - **decline** when out of scope / duplicate / low value → post a courteous rationale.
   - **accept** when it's a well-formed, in-scope feature → call **`POST /v1/commands/propose`** (`main.py:4924`, `intake.propose_command:220`) with the feature summary as the command, producing an LLM plan of dependency-sequenced phases (the decomposer). The proposed plan id is linked back in the comment. Dispatch (`/v1/plans/{id}/dispatch`) is **not** automatic — it stays behind the decision/human gate (§3.5) so a community ask can't self-authorize a full build.
3. **Comment back** via `create_issue_comment` with the verdict, the reasoning, and (on accept) a link to the proposed plan / decision for the maintainer.
4. **Persist** the verdict as a decision (§3.5).

### 3.4 PR-REVIEW agent (pull requests)

Triggered by `github.pr_opened` (and `pull_request.synchronize` for pushes to an open PR). This is the "code-review on developer submissions + work with the branch owner to recommend whether the feature adds value" ask.

1. **Fetch the diff** via `get_pull_request_diff`.
2. **Review the code.** Run the existing **`code_agent` / LLM** review over the diff (correctness, obvious defects, style, test coverage) to produce a set of inline findings `{path, line, body}`. Reuse the engine's review roles rather than a new reviewer.
3. **Post inline review** via `create_review(pull_number, body=<summary>, event="COMMENT", comments=<inline findings>)`.
4. **Value/ROI recommendation to the branch owner.** A second LLM pass answers "does this feature add value and is it worth merging?" — scope vs. project direction, maintenance cost, duplication, alignment. Post it as a top-level comment addressed to the PR author/branch owner (`create_issue_comment`), and, crucially, feed it to the **human gate** as the recommendation on a decision.
5. **No auto-merge.** The agent never calls `merge_pull_request`. Merge remains off by default and a human action; the review + recommendation only inform the maintainer's decision.

### 3.5 Persist verdicts as decisions (audit + human gate)

Both agents terminate by writing a **`decision`** via `create_blocking_decision` (`core.py:96`):

- **Triage:** `question="Accept community feature request #<n>?"`, `recommendation={verdict, feature_summary, proposed_plan_id?}`, `reason=<classification rationale>`, `risk_tier=2`, `default_action="hold"`. Approving the decision is what authorizes `dispatch_plan`.
- **PR review:** `question="Merge community PR #<n>?"`, `recommendation={value_verdict, roi_notes, inline_finding_count, ci_status?}`, `default_action="hold"` (never a default-merge). Approving is a human choice that then triggers the existing merge path.

Because these ride the same `decisions` table and `DecisionService` (`decisions.py:28`, `expire_overdue:39`, digests), community-originated verdicts appear in the same maintainer review queue, expire safely, and are fully auditable — satisfying "the agent triages and decides" *and* "human-approvable."

### 3.6 OSS framing

This is the community front door. The webhook endpoint, the two agents, the adapter methods, and the config knobs ship **in the open-source engine** (they depend only on the App identity, the event bus, the intent router, and the decisions service — all engine components). A maintainer enables it by installing the BOSS GitHub App on their repo and setting `BOSS_GITHUB_WEBHOOK_SECRET`; from then on the repo's own issues/PRs are the interaction surface, with no proprietary control-plane dependency.

---

## 4. Implementation Tickets

### T1 — GitHub inbound webhook endpoint with HMAC verification
**Scope.** Register GitHub as a source on the spec-00 inbound receiver: new `POST /v1/webhooks/github`, raw-body HMAC-SHA256 verification against `X-Hub-Signature-256`, event-type routing from `X-GitHub-Event`, delivery de-dupe on `X-GitHub-Delivery`, self-sender loop-guard, and normalization → `emit_event("github.*")`. Add `github_webhook_secret` config.
**Key files.** `apps/api/app/main.py` (new route near `/v1/comms/inbox/ingest:7871`), `apps/api/app/config.py:455-462` (new `github_webhook_secret` field), `apps/api/app/services/github_webhook.py` (new — verify + normalize), spec-00 receiver/dispatcher.
**Acceptance criteria.**
- [ ] Valid signature over raw body accepted; tampered body / wrong secret / missing header → 401.
- [ ] Signature is checked **before** JSON parsing; endpoint returns 503 when `github_webhook_secret` is unset.
- [ ] `issues`, `issue_comment`, `pull_request`, `pull_request_review` produce the corresponding `github.*` event; all other types → 204 no-op.
- [ ] Redelivered `X-GitHub-Delivery` is idempotent (no duplicate event/agent run); events from BOSS's own App sender are ignored.
- [ ] Handler returns promptly (agent work is off the event, not inline).
**Size.** M

### T2 — Adapter methods: create_issue_comment, create_review, get_pull_request_diff
**Scope.** Add the inbound-response write methods (+ diff read) to `GitHubAdapter`, reusing `_request`/`_headers`/App token.
**Key files.** `apps/api/app/services/github_adapter.py` (after `merge_pull_request:223`); dataclasses near `:45-60`.
**Acceptance criteria.**
- [x] `create_issue_comment(issue_number, body)` posts a comment on issues and PRs.
- [x] `create_review(pull_number, body, event, comments)` posts a review with inline `{path, line, side, body}` comments; defaults to `event="COMMENT"`.
- [x] `get_pull_request_diff(pull_number)` returns the unified diff (diff media type).
- [x] All three authenticate via the existing App installation token path (`_auth_token:73`); no new credential.
- [x] Unit tests with mocked httpx cover success + GitHub error surfacing.

**Implementation evidence (BOSS-85).** `GitHubAdapter` now returns typed comment/review results, validates review events, and extends its existing request seam for raw diff media without bypassing App/PAT authentication. `test_github_community_adapter.py` covers all three success paths, scoped App authentication, inline review payloads, invalid events, and GitHub HTTP errors.
**Size.** S

### T3 — Triage agent (classify → decide → propose → comment)
**Scope.** Subscriber on `github.issue_opened` / `github.issue_comment`: classify via the spec-07 intent router, decide accept/decline/needs-info, on accept call `/v1/commands/propose`, comment the verdict back.
**Key files.** `apps/api/app/services/github_triage_agent.py` (new), spec-07 intent router, `apps/api/app/services/intake.py:220` (`propose_command`), `github_adapter.create_issue_comment` (T2).
**Acceptance criteria.**
- [ ] Feature-request issue → verdict in {accept, decline, needs-info}; non-feature intents get an appropriate reply and stop.
- [ ] needs-info posts specific clarifying questions and re-enters triage on the follow-up comment.
- [ ] accept calls `propose_command` and links the resulting plan id in the comment; **dispatch is not automatic** (gated by T5 decision).
- [ ] Every path posts exactly one verdict comment; agent never triages its own comments (loop-guard honored).
**Size.** M

### T4 — PR-review agent (diff review → inline comments → value/ROI recommendation)
**Scope.** Subscriber on `github.pr_opened` / `pull_request.synchronize`: fetch diff, run `code_agent`/LLM review, post inline `create_review`, post a value/ROI recommendation to the branch owner, feed the human gate. No auto-merge.
**Key files.** `apps/api/app/services/github_pr_review_agent.py` (new), `apps/api/app/services/code_agent.py` (review roles), `github_adapter.get_pull_request_diff` + `create_review` + `create_issue_comment` (T2).
**Acceptance criteria.**
- [ ] Opened PR produces inline review comments anchored to `{path, line}` in the diff.
- [ ] A top-level value/ROI recommendation is posted addressed to the branch owner (does this add value / worth merging).
- [ ] Review uses `event="COMMENT"` — never APPROVE/REQUEST_CHANGES and never `merge_pull_request`.
- [ ] Recommendation is handed to the human gate (T5); merge stays off by default.
- [ ] Re-review on `synchronize` doesn't duplicate prior still-valid inline comments (idempotent per head sha).
**Size.** L

### T5 — Persist triage + review verdicts as decisions (audit + human gate)
**Scope.** Both agents write their verdict/recommendation via `create_blocking_decision`; approving a triage decision authorizes `dispatch_plan`, approving a PR-review decision authorizes the existing merge path.
**Key files.** `apps/api/app/services/core.py:96` (`create_blocking_decision`), `apps/api/app/services/decisions.py:28`, `apps/api/app/main.py:5195` (`dispatch_plan`).
**Acceptance criteria.**
- [ ] Triage accept and PR-review each create one `decisions` row with `default_action="hold"` (never default-dispatch / default-merge).
- [ ] recommendation payload carries the verdict, rationale, and linked plan/PR ids.
- [ ] Approving a triage decision dispatches the proposed plan; approving a PR-review decision is a prerequisite for merge.
- [ ] Verdicts appear in the standard maintainer decision queue/digest and expire safely.
**Size.** M

### T6 — App permissions, config docs, and enablement
**Scope.** Bump the GitHub App manifest permissions (`issues: write`, `pull_requests: write`, subscribe to Issues/Issue comment/Pull request/Pull request review events), document `BOSS_GITHUB_WEBHOOK_SECRET` and the webhook URL, and add an enablement note in OSS docs.
**Key files.** GitHub App manifest / setup docs, `apps/api/app/config.py` (field doc), `docs/` OSS onboarding.
**Acceptance criteria.**
- [x] App requests `issues:write` + `pull_requests:write` and subscribes to the four event types.
- [x] Docs describe installing the App, setting the canonical webhook URL to `/v1/webhooks/inbound/github`, and the shared secret.
- [x] With App uninstalled / secret unset, the feature is fully inert (503, no crashes).

**Implementation evidence (BOSS-89).** `.github/boss-app-manifest.json` declares the least-privilege community permissions and four subscriptions. The webhook runbook documents installation, source-specific secret injection, repository mapping, current staged event support, and the canonical spec-00 endpoint. An unconfigured source now returns 503 before parsing or persistence; installation-token requests include `issues: write` and are unit-tested.
**Size.** S

---

## 5. Dependencies, Sequencing & Priority

**Hard dependencies.**
- **Spec 00 (event/webhook foundation)** — the signed inbound receiver + event-dispatch fan-out. T1 registers GitHub as a source on it; T3/T4 subscribe to the emitted `github.*` events. Without spec 00 there is no place for an inbound webhook to land or a dispatcher to run agents off events. **Must land first.**
- **Spec 07 (orchestration agent)** — owns the **intent router** and is the primary consumer of the **decomposer** (`/v1/commands/propose`). This spec **shares** the intent router (T3 classification) and reuses the same decompose-then-dispatch path. Coordinate the router interface with spec 07; do not fork a second classifier.

**Already-present building blocks (no new dependency):** `github_app_adapter` installation token (`:94`), `intake.propose_command`/`dispatch_plan` (`intake.py:220`/`:116`), `create_blocking_decision` + `DecisionService` (`core.py:96`, `decisions.py:28`), `code_agent` review roles.

**Suggested order:** spec 00 → T1 (endpoint) → T2 (adapter methods, parallel with T1) → T5 (decisions rail) → T3 (triage) → T4 (PR review) → T6 (permissions/docs). T3 and T4 can proceed in parallel once T1/T2/T5 exist; both need the spec-07 router for T3.

**Priority: P1-oss-launch.** This is the community front door for the OSS release; it should be live at or shortly after the repo goes public so early adopters have a responsive agent rather than a silent repo. It is P1 (not P0) because it is additive to a working engine and gated behind opt-in App installation.

---

## 6. Open Decisions

- **Feasibility of a "fully in-GitHub" agent.** The agent lives in GitHub as an *interaction surface*, but the compute (LLM, decomposer, decisions) runs in the BOSS API — GitHub Apps cannot host arbitrary agent loops. Is that acceptable, or does the OSS story require a zero-infra mode (e.g., a GitHub Action) for maintainers who don't run the API? Recommend: API-hosted for launch; Action-based lightweight triage as a later option.
- **Abuse / rate-limits / cost.** A public repo means anyone can open issues/PRs and thereby trigger LLM calls. Need per-sender and per-repo throttling, a spam/off-topic fast-path that never reaches the expensive decomposer, and a budget ceiling (the engine's budget governor already exists — decide how it applies to community-triggered work). Open: whether first-time contributors are auto-triaged or held for a maintainer.
- **Least-privilege permissions.** The App needs `issues:write` + `pull_requests:write` on top of `contents`/`pull_requests`. For public repos, granting an App write on issues/PRs is a real trust ask — document exactly what the agent will and won't do (comments/reviews yes; merge/close no by default). Decide whether review-only (no comment-write) is a supported reduced mode.
- **Auto-dispatch vs. hold on accept.** This spec holds dispatch behind a decision so a community ask can't self-authorize a full build. Confirm maintainers want that friction vs. an allowlist of trusted requesters who can trigger auto-dispatch.
- **Handling forked-PR security.** PRs from forks run untrusted code; the PR-review agent reviews the **diff** (safe) but must **not** build/run fork code in a privileged sandbox off a webhook. Confirm the review path never triggers preview/exec on untrusted PRs.
- **Merge stays off by default — confirm the human-gate UX.** The value/ROI recommendation lands as a decision; verify maintainers can approve/override from the existing decision queue and that approval cleanly triggers the existing `merge_pull_request` path (`github_adapter.py:223`).
