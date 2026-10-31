# Connecting an outside agent (KG P6b)

An outside agent is a bank's own HTTP-hosted service, running under the bank's own
infrastructure, that BOSS calls as the captain of one workflow stage. BOSS keeps the scope
boundary, the postcondition contract, and the audit trail; the bank keeps the model, the
judgement, and the uptime of its own process.

## What a bank implements

The agent is a single HTTP endpoint that accepts a `POST` with this body:

```json
{
  "run_id": "wr_...",
  "step_id": "step_compliance_analyst",
  "input": { "...": "whatever the step's input contract carries" },
  "contract": { "...": "the step's declared reads/writes" },
  "kg": {
    "mcp_url": "https://your-boss-install/mcp",
    "token": "boss_ext_...",
    "expires_at": "2026-10-31T12:00:00Z"
  }
}
```

and returns the step's typed output as JSON, matching the contract's declared writes (for
example, an `agent_decision` node for an `Assessment` contract).

A `POST {"ping": true}` to the same endpoint, with the same `Authorization: Bearer <secret>`
header, is BOSS's reachability check (the console's "Test" button) and must answer `2xx`.

### The token

- **Scope**: the `kg.token` is minted fresh per run/step, scoped to exactly that stage's
  contract (its declared read kinds and write kinds) — a tool or kind the contract did not
  declare is refused by the KG MCP server before any graph access happens.
- **Lifetime**: short TTL, capped at one hour regardless of the step's own timeout.
- **Revocation is immediate**: suspending or removing the agent from the console revokes
  every live token it holds right away, at the token layer — not at the next expiry. The next
  call that token makes fails with `auth_failure`/`token_refused`, and the run that depended on
  it fails closed.
- Use the token only against `kg.mcp_url`'s `kg_find`/`kg_get_node` (reads) and
  `kg_write_agent_decision`/`kg_write_coverage_gap` (writes) tools. It carries no other reach.

### Refuse plainly, never invent a verdict

An agent with no model configured, or one it cannot reach, must refuse — a non-2xx response
or a typed `{"error": "..."}" the run treats as a failed step — rather than guessing at the
judgement. BOSS's `tools/sample_external_agent.py` implements exactly this rule.

## What BOSS guarantees

- **Scope**: the token your agent is handed cannot read or write outside the assigned stage's
  contract.
- **Postcondition**: the stage is only considered complete when the contract's declared write
  actually lands in the knowledge graph; a response that skips it fails the step.
- **Trace**: every `kg_find`/`kg_get_node`/`kg_write_agent_decision`/`kg_write_coverage_gap`
  call your agent makes with its token is recorded against the run, and shown in the console's
  agent read/write view after the run — not a raw dump of your HTTP body.
- **Audit**: token mint and revoke are both written to BOSS's identity audit log.
- Your endpoint's own uptime, scaling, and deploys are **your** infrastructure — BOSS calls it
  synchronously under the step's timeout and does not redeploy or manage your process.

## Running the sample agent

```bash
MODEL_BASE_URL=https://your-openai-compatible-endpoint/v1 \
MODEL_API_KEY=sk-... \
HOST=0.0.0.0 PORT=8765 \
python3 tools/sample_external_agent.py
```

With `MODEL_BASE_URL` unset, the sample refuses every request plainly and writes no decision —
this is the expected, safe default, not a bug.

Register it from the console (Agents → Outside agents → Add outside agent), point its endpoint
at wherever this process is reachable from your BOSS install, click **Test**, and assign it as
the captain of a contract-bearing stage (for example, the Compliance Analyst stage of
`banking_regulatory_compliance`).

## A2A

[A2A](https://a2a-protocol.org/) support is planned but not built yet. This HTTP shape is the
only one a bank implements today.
