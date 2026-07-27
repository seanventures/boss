# Proxy-gaming guardrails: how BOSS handles it, where the gaps are — and the reverse problem we're fixing first

---

## 1. Failure mode problem statement -

An agent's internal "done" is whatever signal is cheapest — tests green, 200 OK, container healthy —
which is only *correlated* with the user-visible behavior working. When the correlation breaks the
agent doesn't notice (it's optimizing the proxy), and the reasoning that should catch it instead
generates the excuse to skip the real check. Advisory controls can't fix it because they're enforced
by the compromised judgment itself. A second failure rides along: proxy-chasing shortcuts destroy
traceability (mixed versions, mislabeled artifacts, unrecorded state).

Your prescription: three inversions — **ground-truth artifact gates that are red-capable**,
**unverifiable ⇒ fail**, **traceability by construction** — wrapped in enforcement the agent can't
route around. Advisory raises the floor; the gate is the thing that says no on its own.

We agree with all of it. Here's the honest scorecard.

## 2. What BOSS already does about it (with receipts)

The architectural move you're asking for — *verification outside the agent's judgment* — is BOSS's
founding design, and it's why this class of failure behaves differently in BOSS than in a
single-agent setup like cheers:

| Your inversion | BOSS mechanism (shipped, in `main`) |
|---|---|
| Enforcement the agent can't route around | **Role separation**: QA writes *frozen* tests **before** implementation, in a separate agent invocation with no shared session; the implementer cannot touch them; **BOSS re-runs the tests itself** — the agent's claim of "tests pass" is never load-bearing. Fail-closed quality gate; PR is human-gated. |
| Red-capable gates | **Mutation gate** (`mutation_gate.py`): deterministic AST mutants are planted in the implementation and the frozen suite must **kill** them; a suite that passes on broken code fails the gate *before* it's trusted to certify anything. This is exactly "a gate that passes on both broken and fixed code proves nothing." |
| Ground truth over proxy | **Visual gate** (`visual_gate.py`) exists because our strongest "it works" signal used to be "a socket answered": it judges *render evidence* (no error overlay, real content, console clean). **Verification targets** (`verification_target.py`) resolve per project type — a web app is verified by `next build` + `tsc` + `eslint` + a Playwright e2e that loads the real route, not by unittest. |
| Post-ship ground truth | The new **AI SRE loop**: `verify` re-watches the *same production signal* that raised an incident; a "green" fix that didn't change behavior is auto-reopened after the stability window. Done = the symptom actually gone, observed, not claimed. |
| Model-specific rationalization (your Fable-vs-Sol observation) | BOSS is model-agnostic with per-step routing, and **no model's self-report is trusted** — the same gates run regardless of which model implemented. That's the only durable answer to "model X rationalizes more than model Y": make the gate model-independent. |

## 3. Honest gaps (real, acknowledged — deliberately deferred)

BOSS three gaps:

1. **Red-proof for bug fixes is advisory, not mechanical.** Our bug framing *tells* the agent
   "write a regression test that FAILS before your fix" — but the pipeline never runs the new tests
   against the base commit to prove it. That's text, i.e., exactly what you showed doesn't work.
   *(Planned: G1 — run new tests on base; no red there ⇒ gate refuses.)*
2. **"Unverifiable" can still become silent green.** Skipped tests don't fail a suite, and at least
   one gate path drops skipped steps from gating entirely. An agent that writes
   `skip("needs staging")` gets green. *(Planned: G2 — evidence-typed acceptance criteria; every
   criterion maps to ≥1 check that ran and produced an artifact; skipped/unmapped ⇒ red;
   "env-limited" becomes a blocking decision or auto-provisions the instrument — never a pass.)*
3. **Traceability is recorded, not enforced.** Receipts/artifacts/decisions give us audit, but
   nothing *refuses* a ship when an artifact doesn't reconstruct to one committed source, or when a
   capability wasn't probed against the actual target. *(Planned: G3 — provenance gate +
   capability probe.)*

G1–G3 are scoped and cheap-ish (G1 especially). We are **not building them yet** — see §4 for why.

## 4. The reverse problem — and why it comes first

While the cheers failure is "agent ships broken code as success," we currently have the mirror
image on BOSS: **the machinery is so tight that work can't ship at all.** Live data from the
"Web app — professional single-page marketing site" task:

| Run | Died in | Class | Error surfaced |
|---|---|---|---|
| `crun_c1abf9c9…` | write_tests (~2.9 min in) | infra / pipeline_exception | `claude agent failed:` *(empty)* |
| `crun_d149cbfc…` | early | infra / pipeline_exception | *(empty)* |
| `crun_86c4ca8f…` | write_tests (**25.5 min** in) | infra / pipeline_exception | *(empty)* |
| `crun_7d721e41…` | write_tests (~2.3 min in) | infra / pipeline_exception | *(empty)* |
| `crun_1c999b31…` | design (~3.2 min in) | infra / pipeline_exception | *(empty)* |

Five runs across 3 days, all on the **microvm substrate**, all dead **before any gate ran** — the
quality gates never saw this task. Three compounding defects:

1. **The real error is swallowed.** `claude agent failed:` with nothing after the colon. The
   Claude-engine step inside the microVM fails and we lose the cause — so every retry is blind.
2. **The failure loop retries the expensive path blindly.** `infra` class ⇒ retry ⇒ re-provision ⇒
   re-run design (3+ min) ⇒ die again. Chained `root_run_id`s show retries of retries. There's no
   circuit breaker that says "same failure 3× on the same root ⇒ stop and page a human with the
   diagnosis."
3. **No degraded-mode fallback kicked in.** The engine has a working host-side substrate
   (`BOSS_GOVERNED_SANDBOX_PROVIDER=local`) — the golden rule of the sandbox contract is that the
   pipeline must run with no sandbox-service present. But substrate selection is static config, so
   a failing microVM path never falls back.

**The principle we're taking from this:** *fail-closed must also fail **loud** and fail **cheap**.*
A gate (or substrate) that blocks silently and loops expensively teaches operators to distrust and
bypass the machinery — which ultimately recreates *your* failure mode by another road. Guardrails
only survive if the experience around a red light is: immediate, diagnosable, and actionable.

**So the order of work is:**
1. **Unblock the task now** — flip the governed working stages to the local substrate (config-only,
   `render`-level change), rerun the web-app task, ship it through the existing gates.
2. **Fix the failure loop** — propagate the real error out of the microVM agent step (an empty
   "claude agent failed:" is a bug, full stop); add a repeated-infra-failure circuit breaker
   (3 strikes on one root ⇒ blocked + human-readable diagnosis, not another silent retry); add
   automatic substrate fallback (microvm fails twice ⇒ finish the run on local, flag it).
3. **Then** G1–G3, one at a time, each with the same UX bar: when the gate says no, the operator
   sees *why*, *what evidence is missing*, and *the one action that goes green* — in the console,
   not in a log.

## 5. BOSS vs cheers on this problem, in one paragraph

Cheers relies on the advisory layer — rules and docs interpreted by the same judgment that's
rationalizing — so a model that rationalizes harder (your Fable observation) degrades the whole
safety story. BOSS's gates are outside the agent: frozen tests it can't edit, mutation-checked so
they're red-capable, re-run by the platform, fail-closed, human-gated at the PR. The two real gaps
(mechanical red-proof for bug fixes, skip-as-failure semantics) are scoped as G1/G2 above. And the
last 3 days taught us the constraint you don't hit until you operate one of these: the gate's
*experience* is part of its security model — a guardrail that loops blindly gets routed around by
humans, which is the same failure you posted, wearing a different hat.

---

*Written from the live run data on goldeneye (project `boss-dogfood`), 2026-07-23. Sources:
`mutation_gate.py`, `visual_gate.py`, `verification_target.py`, `governed_code_task_service.py`,
`reliability.py` (verify loop), code-task runs `crun_c1abf9c9…` → `crun_1c999b31…`.*
