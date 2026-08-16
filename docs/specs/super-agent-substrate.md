# One agent runtime, two surfaces

## The problem

The console agent has 12 hand-written tools (8 read, 4 write). It cannot answer "why did this
deadletter", "what changed between these two runs", or "what is in the preview log", because
those answers live in Postgres, in container logs, and in the repo — and each new question
needs a new tool. Twelve becomes twenty and still does not cover the next question. Tools do
not scale to open-ended questions; a substrate does.

BOSS already runs the substrate. The BUILD agent is `claude-code` in a sandbox with the repo
checked out and shell access — that is what `CLAUDE_CODE_OAUTH_TOKEN` is for. So BOSS operates
a full agent harness and a toy, and the toy is the one answering operator questions.

## The shape

**One runtime, two surfaces.** The build agent and the console agent become the same harness
with different starting prompts and different WRITE scopes. Not two codebases.

| | build surface | console surface |
|---|---|---|
| substrate | sandbox + shell + repo | sandbox + shell + repo + BOSS repo |
| reads | app repo | app repo, BOSS repo, read-only DB, logs |
| writes | commits, PRs (governed) | `change_existing_app`, `create_followup_task` (governed) |

Read broadly; write only through the gates that already exist. That boundary is the thing
worth defending — not read restrictions, which only produce unanswerable questions.

## Build order

1. **Session substrate.** Give the console agent a sandbox session: BOSS repo and the app repo
   checked out, `bash` available, working directory scoped to the session. Replaces
   `list_repo_files` / `read_repo_file`, which collapse into `grep` and `cat`.
2. **Read-only DB.** A Postgres role with SELECT only, and a `query_db` tool. This is the gap
   that makes every failure question unanswerable today. Note the MCP surface already exposes
   `mcp__boss__query_db` — the console agent is the only client without it.
3. **Logs.** Container logs and preview logs. Three separate misdiagnoses of one preview bug in
   a single session traced to nobody — including the human operator's assistant — being able to
   read `docker logs`.
4. **Keep writes governed.** Actions stay explicit tool calls that route through the approval
   path. A shell that can `git push` is not the goal; a shell that can *diagnose* is.

## Deployment posture

Single tenant, internal, not exposed. For the community release the deployer owns the data, so
full read access to their own instance is correct and expected. The constraint is not
confidentiality between tenants; it is that an agent must not take irreversible action without
passing a gate.

## Done means

The console agent can answer, unaided:
- "Why did the last build of this app fail?" (DB + logs)
- "What is different between this run and the one that worked?" (repo + DB)
- "The preview times out — what is it actually running?" (repo + logs; the bug that took three
  attempts and two deploys to find in the 2026-08-16 session)
- "Fix it" — and start a governed run from that conclusion.
