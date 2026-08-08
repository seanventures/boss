# BOSS Engine Design-Partner Onboarding

**Plane:** BOSS-292  
**Audience:** a developer evaluating the private BOSS Engine repository before
the public release.  
**Outcome:** a partner can run the engine locally, inspect it through the CLI or
MCP, complete one governed sample change against an approved sample repository,
and return evidence-backed feedback.

This is a private design-partner program, not a promise that the engine is
publicly available or that hosted BOSS services are configured. The public
release remains gated by the licensing decision and the public-flip checklist.

## 1. Request access and establish the boundary

The BOSS owner supplies:

- access to the private engine repository and its pinned release revision;
- a **canonical sample repository** and its default branch;
- the least-privilege repository credential or GitHub App installation needed
  for a draft PR;
- a support contact and the feedback issue location;
- an approved budget/model configuration if the partner will run a code task.

The partner confirms:

- their local Docker and PostgreSQL use is permitted;
- the sample repository contains no production secrets or customer data;
- they will review any proposed write before approval or merge;
- they will not point the evaluation at a production provider account.

Do not paste API keys, GitHub App private keys, provider credentials, or
customer data into a feedback issue. Use the configured secret channel for
credentials and report only the SecretRef name or redacted error evidence.

## 2. First 15 minutes: run the engine locally

From the repository root, follow the Docker quickstart in
[README.md](./README.md#docker). The minimal proof is:

~~~bash
docker build -f engine/Dockerfile -t boss-engine .

docker network create boss
docker run -d --name boss-db --network boss \
  -e POSTGRES_DB=boss -e POSTGRES_USER=boss -e POSTGRES_PASSWORD=boss postgres:16
export DB='postgresql+psycopg://boss:boss@boss-db:5432/boss'

docker run --rm --network boss -e DATABASE_URL=$DB boss-engine bootstrap
docker run --rm --network boss -e DATABASE_URL=$DB boss-engine --project boss-dogfood status
docker run --rm --network boss -e DATABASE_URL=$DB boss-engine tools
~~~

Success means the bootstrap command completes and both status and tools return
structured output. A database connection failure is not a successful
installation; capture the redacted failure and stop before attempting a task.

When finished, remove the evaluation resources:

~~~bash
docker rm -f boss-db
docker network rm boss
~~~

## 3. Connect an MCP client in read-only mode

Start the HTTP MCP server locally:

~~~bash
docker run -d --name boss-mcp --network boss -p 8791:8791 -e DATABASE_URL=$DB \
  boss-engine serve-mcp --http --read-only --host 0.0.0.0
~~~

Configure the approved MCP client with the local HTTP endpoint
`http://localhost:8791/mcp`. The endpoint speaks JSON-RPC over HTTP; this
request is a client-independent proof before configuring a richer client:

~~~bash
curl -sS http://localhost:8791/mcp \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
~~~

Run tools/list and one read request such as project_status. In read-only mode,
a write tool such as create_code_task must be unavailable; that denial is
expected evidence that the safety boundary is active. Client-specific
walkthroughs are maintained as a separate BOSS-290 launch asset so this
private-engine onboarding contract remains independently usable.

Stop the server when the evaluation ends:

~~~bash
docker rm -f boss-mcp
~~~

## 4. Complete the canonical sample change

This proof requires the BOSS owner to select the canonical sample repository
and configure its registered repository binding, model lane, budget, and
least-privilege credential. Do not substitute an arbitrary production
repository.

Use the following acceptance contract for the first sample:

1. The request is specific and has written acceptance criteria and test
   commands.
2. BOSS creates a governed code task against the owner-supplied sample
   repository and default branch.
3. The worker produces a draft PR; the partner reviews the diff and the test
   evidence before any merge.
4. The task preserves the audit trail: task/run identity, approval or block,
   budget/reservation evidence when applicable, receipt/effect evidence, test
   result, and draft-PR URL.
5. A failed precondition, policy check, model entitlement, or repository
   permission remains visible as a block, decision, retry, or deadletter. It
   must never be represented as a completed sample.

The appropriate task-creation boundary is the create_code_task tool. It is a
write and must be confirmation-gated in an interactive chat or explicitly
authorized in the configured control plane. Do not expose it through a
read-only MCP server.

## 5. Return a useful feedback record

File one feedback item per distinct friction point in the owner-provided issue
tracker. Include:

~~~text
Title: [BOSS design partner] <short outcome or blocker>
Environment: OS, Docker version, engine revision, client (CLI/MCP), redacted DB setup
Step: exact onboarding step and command/action
Expected: what the guide said would happen
Observed: result, redacted logs, screenshot, or MCP response
Impact: blocked / serious friction / minor friction
Evidence: task/run ID, draft PR URL, receipt/decision ID where applicable
Suggested improvement: optional
~~~

For feedback on a running or completed governed code task, use the
send_feedback tool with the task run ID so the feedback stays linked to the
work lineage. For a setup or documentation defect, use the issue tracker
instead; do not attach secrets or source code that the owner did not authorize.

## Partner exit checklist

- [ ] Local bootstrap, status, and tools succeeded.
- [ ] Read-only MCP lists tools and rejects a write tool as expected.
- [ ] The canonical sample repo and credential scope were explicitly supplied.
- [ ] One governed sample task reached either a reviewed draft PR with evidence
      or a visible, reproducible block.
- [ ] Feedback was filed with redacted evidence.
- [ ] Evaluation Docker resources and local credentials were removed or
      revoked according to the owner’s instructions.

The BOSS owner records the partner result in BOSS-292. The private design
partner phase exits only after at least two external partners self-serve this
path to a completed sample task and the resulting friction has been triaged.
