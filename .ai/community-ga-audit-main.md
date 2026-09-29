# Community GA audit: main vs v1.0-rc1

Audit only. No merge, no push, no tag move, no product-code edit.

- Worktree: `/Volumes/DataSSD1/boss-community-ga-integration/swarms/audit-main`
- Branch: `ai/grok/ga-audit-main`
- Fetch: `git fetch --all --prune` exited 0 with no output. No fetch error.
- Remote: `origin` only.
- Tags were not moved. `v1.0-rc1` still peels to `b194c65c3028b80cd621fc187280f4d79e84df02`. `v0.14.0` still peels to `d8069168cc85a88f4551edc8ff4c7361ebc19519`.

## 1. SHAs

| Ref | SHA |
| --- | --- |
| `v1.0-rc1` annotated tag object | `04bb2e79f84acb1c3d7074f1617f4c6b37aa452d` |
| `v1.0-rc1` peeled commit (`v1.0-rc1^{}`) | `b194c65c3028b80cd621fc187280f4d79e84df02` |
| `HEAD` (`ai/grok/ga-audit-main`) | `b194c65c3028b80cd621fc187280f4d79e84df02` |
| `origin/main` | `6407e2f9e8e24a26a3f63a9902a4827661540a18` |
| local `main` | `d8069168cc85a88f4551edc8ff4c7361ebc19519` |
| `origin/ai/codex/boss-293-community-integrate` | `371a1c72a1fd49f3336f5790b55407d51886412a` |

`HEAD` is the peeled `v1.0-rc1` commit. Subject: `release: name the community candidate v1.0-rc1` (2026-09-29 11:43:41 -0400, Sean Heiney).

`origin/main` subject: `Every lane's model list follows its own engine, and a stale model is cleared` (2026-09-28 17:33:45 -0700, Paramaguru Gnanaguru).

## 2. Does the tag contain origin/main?

Yes. `origin/main` is an ancestor of `v1.0-rc1` (`git merge-base --is-ancestor origin/main v1.0-rc1` exited 0).

`git log --oneline v1.0-rc1..origin/main` is empty. No commits on `origin/main` are missing from the tag, so there are no authors to list.

## 3. Is local main the same commit as origin/main?

No. They have diverged. Neither is an ancestor of the other.

- Merge base: `7732c04082bdc8d6584ad45c3bf5d75e7692b565` (`docs(engine): keep onboarding MCP proof self-contained`, 2026-08-08 08:34:22 -0400).
- Local `main` is 346 commits ahead of `origin/main` and 1272 commits behind it (`git rev-list --left-right --count main...origin/main` = `346\t1272`).
- Local `main` is the `v0.14.0` commit `d8069168cc85a88f4551edc8ff4c7361ebc19519` (2026-08-15 16:00:30 -0400).
- `origin/main` is the newer tip, but it does not contain local `main`.

## 4. Release commits not on origin/main

443 commits are on `v1.0-rc1` and not on `origin/main` (`git rev-list --count origin/main..v1.0-rc1`). That is the expected candidate-ahead set. SHAs are not listed.

## 5. Fetched after 2026-09-28 and not in the tag

Cutoff: reflog time on or after 2026-09-29 00:00:00 -0400, tip commit not contained in `v1.0-rc1^{}`.

One ref:

| Ref | SHA | When fetched | Author | Subject |
| --- | --- | --- | --- | --- |
| `refs/remotes/origin/cursor/critical-bug-investigation-3dc0` | `a3148c12d317c18c0caf8738667e0d020dbff3b9` | 2026-09-29 09:43:56 -0400 (`fetch origin --prune: storing head`) | Cursor Agent \<cursoragent@cursor.com\> | Deleting a finished subworkflow left its parent waiting forever |

Commit time: 2026-09-29 05:08:03 +0000. `git rev-list --count v1.0-rc1..origin/cursor/critical-bug-investigation-3dc0` is 1. It is not on `origin/main`.

The only other remote-tracking ref whose latest reflog is after that cutoff is `refs/remotes/origin/ai/codex/boss-293-community-integrate` at `371a1c72a1fd49f3336f5790b55407d51886412a` (2026-09-29 10:08:53 -0400, `update by push`). That commit is inside `v1.0-rc1`.

`git log --all --since='2026-09-29' --not v1.0-rc1^{}` returns only `a3148c12d317c18c0caf8738667e0d020dbff3b9`. No other commit with an author or committer date on or after 2026-09-29 is outside the tag.
