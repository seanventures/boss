# Local git remote and local review (BOSSAPP-948)

Design for approval. Wireframe: [5-local-change-review.html](wireframes/agentic-factory/5-local-change-review.html).

**Today:** the bare repository is `/var/lib/boss/repos/<project>/<repo>.git` in the volume `boss_community_repos`. The page is read-only. People's line comments exist only per run (`finalize_ctx.review_comments`), which repair rounds already read.

## 1. Git smart HTTP, served by the API

- **Route:** `/git/<project>.git/*` is the primary local repo. `/git/<project>/<repo>.git/*` serves the others. The web proxies `/git/*`, so the URL is `http://localhost:3000/git/helpdesk-laptop.git`.
- **Engine:** `git http-backend`, streamed. Git and the volume are already in the API container; dulwich would reimplement packs and hooks.
- **Read:** `repository:read`.
- **Push:** a new `repository:push` (O/A/P). It is checked at `info/refs?service=git-receive-pack`, so git asks for credentials before it uploads.
- **Default branch:** a `pre-receive` hook refuses pushes to the default branch, tags, force-pushes, and `boss/*` branches while a run builds them. Main moves only through the governed merge, which uses `local_repo.merge` and never goes through receive-pack. The hooks come from the image through `core.hooksPath`, so no clone can change them.
- **Auth:** the console uses the session cookie. Git clients use HTTP Basic with a `boss_pat_` token as the password; the host tools use their service-account key. Basic is accepted on `/git/*` only. `boss clone` stores the token through `git credential approve` (the keychain), never in the URL or `.git/config`.
- **Offline:** the web and API bind to 127.0.0.1. `/git/*` also refuses non-loopback clients unless `git_remote.reach=network`.
- **Limits:** `receive.maxInputSize` of 200 MB.

**file:// (weaker).** The volume is not visible from the host (it sits inside Docker Desktop's VM), so this needs a bind mount such as `~/.boss/repos`. It has no auth and no identity on a push, its hooks can be deleted by any host user, it hits uid mismatches, and it leaves no audit trail. It would be a read-only clone at most.

## 2. A push becomes a change set

`post-receive` runs `boss_engine.services.local_git_hooks` inside the API container, with the pusher's principal in its environment. For each updated non-default branch it opens or updates the `local_change_sets` row:

- author = the pusher, label `pushed`, the new `head_sha`;
- the sandbox checks are queued;
- the review decision is raised;
- it prints `remote: BOSS opened change #N <url>`.

A reconciler on the push-pending tick catches refs changed any other way.

## 3. Comments: one store

`local_change_set_comments(id, change_set_id, project_id, path, line, end_line, side, commit_sha, author, author_kind, severity, body, state open|addressed|disputed|resolved|outdated, reply_to, origin console|cli|git_notes, run_round, response_commit, github_comment_id, timestamps, resolved_by)`

- **Writers:** the diff, the Code tab, `boss review comment|list|reply|resolve` (a new `boss.sh` subcommand), and MCP `add_review_comment` on a local change.
- **Panel notes:** these stay in `local_change_set_notes`.
- **Anchor:** (commit, path, side, line). When the head moves, comments are re-mapped by diff. A comment whose line changed becomes `outdated` and stays visible.
- **Request changes** (`POST …/request-changes {note}`) revises the pending review decision. `review_pull_request` joins `REVISE_CONSUMED` for local URLs. The revision starts a fix build on the branch; a pushed change with no run gets a new code task on it.
- **Required inputs:** each repair round copies the open human comments, with their ids, into `finalize_ctx.review_comments`, people's first, through the existing `render_for_implementer`. `parse_responses` writes the agent's reply ("Changed in commit abc: …") and sets the state to addressed or disputed.
- **Merge:** the round cannot raise its merge decision while a required comment has no reply, and Merge is held while any human comment is open. A person resolves or reopens a thread.
- **Git notes (optional):** `refs/notes/boss-review` carries JSON lines. A push imports them, and BOSS exports comments to it for `git log --notes=boss-review`.

## 4. File view API

`GET …/local-changes/{n}/tree?ref=head|base` returns paths with M/A/D status and comment counts.

`GET …/local-changes/{n}/file?path=&ref=` reads the file, capped at 1 MB, and reports binary files and the language. Refs are limited to the change's head, its base or a branch commit.

The change view gains `where`: the storage path, volume, branch, head, `remote_url` and `working_copies[]` (recorded by `boss clone`). Shiki highlights in the browser, lazy-loaded.

## 5. Publish

`local_publish` step 3 posts one COMMENT review per PR:

- A comment on the head commit that falls inside a diff hunk becomes an inline comment (`line`, `side` RIGHT/LEFT).
- Outdated comments and comments outside a hunk go in the review body with the old line quoted.
- Replies post as replies.
- Resolved threads are resolved through GraphQL `resolveReviewThread`.
- Each body starts `**Person** · local review · date`, because the App is the author on GitHub.
- `github_comment_id` keeps a re-run idempotent.

## 6. Identity (`tests/identity/matrix.yaml`)

| Route | Permit | Roles |
|---|---|---|
| GET `/git/{project_id}.git/info/refs` | repository:read (+ push for receive-pack) | OAPVU |
| POST `/git/{project_id}.git/git-upload-pack` | repository:read | OAPVU |
| POST `/git/{project_id}.git/git-receive-pack` | repository:push (new) | OAP |
| GET `…/local-changes/{number}/tree`, `/file`, `/comments` | code_task:read | OAPVU |
| POST `…/comments`, `…/comments/{id}/reply`, `…/comments/{id}/resolve` | code_task:steer | OAP |
| POST `…/local-changes/{number}/request-changes` | decision:approve, people only | OAP |
| POST `/v1/projects/{project_id}/local-repo/working-copies` | repository:read | OAP |

## 7. Tests

- Clone and pull with a token, and 401 without one; a Viewer clones but cannot push.
- Pushes to main, tags, forced pushes, and branches a run is building are refused with words.
- A pushed branch opens, then updates, a change set.
- Console, CLI and notes comments land in one row.
- Re-mapping and `outdated`.
- Request changes starts a round whose prompt carries the human comments first.
- RESPONSE lines become replies, and the merge decision waits for them.
- Tree and file refuse traversal and unknown refs.
- Publish mapping, with a re-run posting nothing twice.
- The token is absent from URLs, logs and `.git/config`.
- The matrix is regenerated.

## 8. Risks

- Streaming large packs through FastAPI and the Next proxy, and the proxy's body limits.
- BOSS writing while receive-pack runs. Ref locks and the run lock cover it.
- The hooks are the only fence on pushes, so they stay in the image.
- A host exposed on a tailnet exposes `/git` unless `reach=local`.
- Line-mapping drift on rewrites.
- GitHub rejects comments outside a hunk; the review body is the fallback.

## 9. Phases

- **P1:** where it lives, the Code tab, console comments, Request changes, comments as required inputs, and Merge held while comments are open.
- **P2:** git remote read, tokens, `boss clone` and working copies.
- **P3:** push and hooks, a branch becoming a change set, `boss review`, and git notes.
- **P4:** Publish carries comments, replies and resolved state.
