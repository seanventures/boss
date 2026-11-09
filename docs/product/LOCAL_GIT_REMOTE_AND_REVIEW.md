# Local git remote and local review (BOSSAPP-948)

Status: design for approval. Wireframe: [5-local-change-review.html](wireframes/agentic-factory/5-local-change-review.html).

**Ask (2026-11-09):** show where a local project's code lives, open files inside BOSS, comment on lines and keep the comments locally so the next run addresses them. Work offline with a plain git client, then publish to GitHub later.

**Today:** the bare repository is `/var/lib/boss/repos/<project>/<repo>.git` in the Docker volume `boss_community_repos` (`local_repo.bare_path`). The change-set page is read-only. Panel reviews live in `local_change_set_notes`. People's line comments exist only per run (`code_task_runs.finalize_ctx.review_comments`, from `add_review_comment`). The repair round already reads them first (`_open_review_comments`) and parses `RESPONSE: n FIXED|DISPUTED`.

## 1. Git smart HTTP, served by the API

- **Route:** `/git/<project>.git/*` is the project's primary local repo. `/git/<project>/<repo>.git/*` serves any other. The web proxies `/git/*` to the API, so the clone URL is `http://localhost:3000/git/helpdesk-laptop.git` on the same origin as the console.
- **Engine:** `git http-backend` (CGI), streamed from FastAPI. Git is already in the API image and the volume is already mounted, so no new dependency. Dulwich was rejected: it is a second implementation of pack negotiation and hooks.
- **Authorization:**
  - `info/refs?service=git-upload-pack` and `git-upload-pack` need `repository:read`.
  - `info/refs?service=git-receive-pack` and `git-receive-pack` need a new `repository:push` (Owner, Admin, Operator). The refs check is inline, so git asks for credentials before it sends a pack.
- **Default branch:** a `pre-receive` hook refuses any update to the default branch and any tag. Main moves only through BOSS's governed merge, which writes with `local_repo.merge` and never goes through receive-pack. The hook also refuses a push to a `boss/*` branch while a run is building it, and refuses non-fast-forward pushes. BOSS installs the hooks through `core.hooksPath`, pointing at a directory in the image, so a clone cannot change them.
- **Auth:**
  - The console uses the BOSS session cookie.
  - Git clients use HTTP Basic with any username and a `boss_pat_` personal access token (scopes `repository:read`/`repository:push`) as the password. The host tools use their service-account key. Basic is accepted on `/git/*` only, and the existing redaction already covers `Basic` values.
  - `boss clone` stores the token through `git credential approve` (the OS keychain), never in the URL or in `.git/config`.
- **Offline:** the web and API already bind to `127.0.0.1`. `/git/*` also refuses a non-loopback client unless the install setting `git_remote.reach` is `network` (default `local`).
- **Limits:** `receive.maxInputSize` (default 200 MB), plus a per-request timeout.

**file:// option (weaker, not the default).** The volume is not visible from the host: on Docker Desktop it sits inside the VM. A file remote therefore needs a bind mount, such as `~/.boss/repos`. That also brings:

- no authentication and no identity on a push;
- hooks a host user can delete;
- uid mismatches between the container and the host;
- no audit trail.

It would serve as a read-only clone at most.

## 2. A push becomes a change set

`post-receive` runs `python -m boss_engine.services.local_git_hooks post-receive` inside the API container, with the API's settings. The pusher's principal is passed in the hook's environment. For each updated non-default branch it opens or updates the branch's `local_change_sets` row:

- author = the pusher, label `pushed`, `run_id` null, `head_sha` updated;
- the sandbox checks are queued;
- it prints `remote: BOSS opened change #N <url>`.

A reconciler, run on the API's push-pending tick, compares refs with the table and catches anything changed another way. A pushed change set gets the same review decision as a built one, so Merge stays a person's decision.

## 3. Comments: one store

```
local_change_set_comments(id, change_set_id, project_id, path, line, end_line,
  side new|old, commit_sha, author, author_kind human|agent, severity, body,
  state open|addressed|disputed|resolved|outdated, reply_to, origin
  console|cli|git_notes|panel, run_round, response_commit, github_comment_id,
  created_at, updated_at, resolved_by)
```

- **Writers:** the diff, the Code tab, `boss review comment|list|reply|resolve` (a new `boss.sh` subcommand that calls the API with the host token), and the MCP `add_review_comment` when the run has a local change set.
- **Panel notes:** these stay in `local_change_set_notes`. The page shows both.
- **Anchor:** (commit, path, side, line). When the head moves, the comment is re-mapped with `git diff` line mapping. If its line changed, it becomes `outdated` and stays visible.
- **Fix round:**
  - **Request changes** (`POST …/local-changes/{n}/request-changes {note}`) answers the pending review decision with *revise*. `review_pull_request` joins `REVISE_CONSUMED` for local URLs. The revision starts a fix build on the same branch. A pushed change set with no run gets a new code task on its branch.
  - At each repair round, open human comments are copied into `finalize_ctx.review_comments` with their stored ids, as **required inputs**, people's first. This reuses `render_for_implementer`, so the rest of the pipeline is unchanged.
  - `parse_responses` writes the agent's reply (`reply_to`, "Changed in commit abc: …") and sets the state to `addressed` or `disputed`.
  - The round cannot raise its merge decision while a required comment has no reply. Merge is held while any human comment is open.
  - A person resolves a thread, or reopens it.
- **Git notes (optional, P3):** `refs/notes/boss-review` holds JSON lines per commit. A pushed note is imported as a comment from the pusher, and BOSS exports comments to the same ref so `git log --notes=boss-review` works offline.

## 4. File view API

`GET …/local-changes/{n}/tree?ref=head|base` returns paths with M/A/D status and comment counts. It uses `list_paths` and `changed_files`.

`GET …/local-changes/{n}/file?path=&ref=` reads the file with `read_file`, capped at 1 MB. It reports binary files and the language. `ref` is limited to the change's head, its base, or a commit on the branch.

`GET …/local-changes/{n}` gains a `where` block:

- the storage path and volume;
- the branch and head;
- `remote_url`;
- `working_copies[]`, recorded by `boss clone`.

Syntax highlighting is done in the browser with Shiki, lazy-loaded.

## 5. Publish

This extends `local_publish` step 3: after each pull request is opened, BOSS posts one COMMENT review.

- A comment on the PR head whose line is in a diff hunk becomes an inline comment (`line`, `side` RIGHT/LEFT).
- Outdated comments, and comments outside a hunk, go in the review body with the old line quoted.
- Replies use `reply_to_review_comment`.
- Resolved threads are resolved through GraphQL `resolveReviewThread`.
- Each body starts with `**Person** · local review · date · origin`, because the App is the author on GitHub.
- `github_comment_id` makes a re-run idempotent.

## 6. Identity (new rows in `tests/identity/matrix.yaml`)

| Route | Permit | Roles |
|---|---|---|
| GET `/git/{project_id}.git/info/refs` | repository:read (+ inline repository:push for receive-pack) | OAPVU |
| POST `/git/{project_id}.git/git-upload-pack` | repository:read | OAPVU |
| POST `/git/{project_id}.git/git-receive-pack` | repository:push (new) | OAP |
| GET `…/local-changes/{number}/tree`, `/file` | code_task:read | OAPVU |
| GET `…/local-changes/{number}/comments` | code_task:read | OAPVU |
| POST `…/comments`, `…/comments/{id}/reply`, `…/comments/{id}/resolve` | code_task:steer | OAP |
| POST `…/local-changes/{number}/request-changes` | decision:approve, people only | OAP |
| POST `/v1/projects/{project_id}/local-repo/working-copies` | repository:read | OAP |

The `<repo>` path variants mirror the first three rows.

## 7. Tests

- Clone, fetch and pull over HTTP with a token, and 401 without one.
- A Viewer can clone but cannot push.
- A push to main, a tag, a forced push, and a push to a `boss/*` branch during a run are each refused with words.
- A pushed branch opens a change set, and a second push updates it.
- A comment from the console, the CLI and git notes lands in one row.
- Re-mapping after a new commit, and `outdated`.
- Request changes starts a fix round whose prompt carries the human comments first.
- RESPONSE lines become replies, and the merge decision waits for unanswered required comments.
- Tree and file at head and base, with path traversal and an unknown ref refused.
- Publish maps inline, outdated and resolved comments, and a re-run posts nothing twice.
- The token never appears in a remote URL, a log or `.git/config`.
- The matrix is regenerated.

## 8. Risks

- **Streaming:** large packs through FastAPI and the Next proxy, and the proxy's body limits.
- **Concurrent writers:** BOSS's commits and receive-pack. Git ref locks cover this, and the run lock prevents races on branches.
- **Hooks:** they are the only push fence, so they must live outside the repo, in the image.
- **Exposure:** a host exposed on a tailnet exposes `/git` unless `reach=local`.
- **Line mapping:** drift on large rewrites.
- **GitHub:** it rejects comments outside a hunk, which falls back to the review body.
- **Untested branches:** a person can push one, and checks still gate Merge.

## 9. Phases

- **P1:** where it lives, the Code tab with tree and file, console line comments, Request changes, comments as the fix round's required inputs, and Merge held while comments are open.
- **P2:** git remote read (clone, pull), tokens, `boss clone` and working copies.
- **P3:** push (hooks, a branch becomes a change set), `boss review`, and git notes.
- **P4:** Publish carries comments, replies and resolved state.
