# Pilot checklist

Five things that would block a pilot, and how to see each one working on your own install.
Ticket: BOSSAPP-1289. The code and tests behind each item are in the
[security review pack](../security/SECURITY_REVIEW_PACK.md).

`$BOSS` below is your install's address, and requests are made as a signed-in admin (or with an
admin API key). Nothing here prints a secret.

| # | Blocker | State today |
|---|---|---|
| 1 | Your own model keys | Ready |
| 2 | Secrets in a vault, never printed | Ready (one setting to check) |
| 3 | Human approval | Ready for production; opt in for development |
| 4 | Audit evidence | Ready |
| 5 | Isolated execution | Sandbox with a network fence; **microVM not yet** |

## 1. Your own model keys

**What it means.** You connect your own provider account (Anthropic, OpenAI, Azure OpenAI,
Bedrock, an OpenAI-compatible endpoint, or a local model). Agents never hold the key: each session
gets a short-lived `boss_mdl_` token that only works on the model API paths, through a local proxy
that adds the key (`engine/boss_engine/services/model_proxy.py`).

**Verify on the install.**
1. **Setup › Models**: connect the key with **Connect**. It makes one small governed call and shows
   **Working** with a receipt ([MODEL_CONNECTIONS.md](../community/MODEL_CONNECTIONS.md#verification-and-live-checks)).
2. Run any small build. Its cost receipt names your provider: `GET $BOSS/v1/receipts`.
3. Use an API key, not a subscription sign-in: a subscription login is copied into the sandbox
   and is not proxied (see the security pack, section 3).

## 2. Secrets

**What it means.** Connection credentials and app secrets are encrypted at rest (AES-256-GCM) and
read only through BOSS. No endpoint returns a secret's value. Logs, error reports and run output
are redacted.

**Verify on the install.**
1. `GET $BOSS/v1/secrets?project_id=<id>` lists metadata only (name, backend, fingerprint, last 4,
   status). There is no variant with values (`apps/api/app/main.py`, `list_secrets_endpoint`).
2. `GET $BOSS/v1/secrets/<id>/access-log` shows who read a secret, and when.
3. Check `.env` holds `BOSS_KMS_MASTER_KEY` (presence only, never print it):
   `grep -c '^BOSS_KMS_MASTER_KEY=.' .env` prints `1`. Without it, the `database` backend keeps
   its key in the same database (`engine/boss_engine/services/secret_backends.py`).

## 3. Human approval

**What it means.** Production deploys and promotions always wait for a person with
`decision:approve` and a human session; an API key or agent cannot approve
(`engine/boss_engine/identity/approvals.py`). Merges need an approver other than the change's
creator. Outside production, agents act by default (`services/autonomy_policy.py`, `dev: act`).

**Verify on the install.**
1. Start any workflow that ends in a production deploy or `promote_config`. It stops at a decision
   card; **Approvals** in the console shows it waiting for you.
2. Try to approve it with an API key: it is refused.
3. If your pilot needs a person on every merge too, set the autonomy policy to `dev: ask` for the
   org or the pilot project. A project may only be stricter than the org.

## 4. Audit evidence

**What it means.** Security events (sign-ins, people, role and policy changes, egress denials,
and more) are kept in an append-only, hash-chained log for 400 days by default. Every agent action
has a receipt.

**Verify on the install.**
1. `POST $BOSS/v1/identity/audit/verify`: the chain verifies, or reports its first break.
2. `POST $BOSS/v1/identity/audit/export`: a JSON bundle with an anchor hash, for your records.
3. `GET $BOSS/v1/activity/export` (CSV or NDJSON) for the activity trail; `GET $BOSS/v1/receipts`
   for agent actions and their cost.

More: [IDENTITY_ADMIN.md › The security audit log](../community/IDENTITY_ADMIN.md#the-security-audit-log).

## 5. Isolated execution

**What it means today.** Each build runs as its own unprivileged user in a throwaway clone,
inside the BOSS container, behind a network fence: it can reach only the BOSS API and the egress
proxy, and the proxy allows only model APIs and package registries (plus what you add). It shares
the host kernel; it is not a VM ([SANDBOX.md](../community/SANDBOX.md)).

**MicroVM: not yet.** `BOSS_MICROVM_CLAIMS_FROZEN=true` is the default. While it is true, BOSS
refuses any microVM execution claim with 422 rather than claim isolation it does not have.

**Verify on the install.**
1. `GET $BOSS/v1/diag/sandbox-roundtrip` answers 422 with the frozen message: BOSS does not claim
   microVM isolation.
2. **Setup** shows the sandbox and engine checks green ([ENGINE_SANDBOX.md](../community/ENGINE_SANDBOX.md#the-engine-check-in-setup)).
3. Run a build whose test tries `curl https://example.com`: the request is denied, and the denial
   is in the egress log and the security audit log ([SANDBOX.md › The egress log](../community/SANDBOX.md#the-egress-log)).

## Not yet

- A recorded demonstration of each item on a preview install.
- An independent agent's re-walk of this checklist on a fresh install.
- MicroVM isolation for builds.
