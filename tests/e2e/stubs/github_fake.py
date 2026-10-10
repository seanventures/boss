"""GitHub's REST API, answered from the same bare repositories `git` clones and pushes to.

WHY BACKED BY REAL GIT. BOSS does not only push: its software factory writes commits through
GitHub's Git Data API (blobs → trees → commits → refs), reads branches and compares them, opens
a pull request and merges it — and the preview and deploy steps then CLONE what was merged. A fake
that returned canned JSON would let the PR "succeed" while the next clone found none of the work,
which is exactly the class of failure that reached a customer ("this app is not in main"). So
every write here is a real git object in the bare repo under fakes/git/, and every read comes back
out of it.

Implemented: repos, branches, git/ref(s), git/commits, git/blobs, git/trees, commits/{sha},
commits/{sha}/status|check-runs, compare, contents (GET/PUT), merges, pulls (create, list, get,
update, merge, update-branch, files, reviews), issue comments, deployments and statuses.
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

_LOCK = threading.RLock()
_ZERO = "0" * 40


def _git(bare: Path, *args: str, input_bytes: Optional[bytes] = None,
         env: Optional[dict[str, str]] = None) -> str:
    full = {**os.environ, "GIT_AUTHOR_NAME": "boss-e2e", "GIT_AUTHOR_EMAIL": "e2e@x.test",
            "GIT_COMMITTER_NAME": "boss-e2e", "GIT_COMMITTER_EMAIL": "e2e@x.test",
            **(env or {})}
    done = subprocess.run(["git", "--git-dir", str(bare), *args], input=input_bytes,
                          capture_output=True, env=full, timeout=60)
    if done.returncode != 0:
        raise GitError(done.stderr.decode(errors="replace").strip())
    return done.stdout.decode(errors="replace").strip()


class GitError(RuntimeError):
    pass


class GitHubFake:
    def __init__(self, remotes, world):
        self.remotes = remotes          # egress.GitRemotes
        self.world = world              # fakes.World (prs live in world.prs)

    # ------------------------------------------------------------------ helpers
    def _check_runs(self, bare: Path, full: str, ref: str) -> list[dict[str, Any]]:
        """The check runs the test told the fakes this commit reports (POST /github/_checks),
        by sha, then by any branch whose head is that sha, then "*". None set: no CI."""
        checks = getattr(self.world, "checks", {}) or {}
        sha = self._sha_of(bare, ref) or ref
        runs = checks.get(sha) or checks.get(ref)
        if runs is None:
            for key, value in checks.items():
                if key != "*" and self._sha_of(bare, f"refs/heads/{key}") == sha:
                    runs = value
                    break
        if runs is None:
            runs = checks.get("*") or []
        out = []
        for i, run in enumerate(runs):
            item = {"id": 9000 + i, "name": "ci", "status": "completed", "conclusion": "success",
                    "head_sha": sha, **run}
            item.setdefault("html_url", f"https://github.com/{full}/actions/runs/{item['id']}")
            item.setdefault("details_url", item["html_url"])
            out.append(item)
        return out


    def _sha_of(self, bare: Path, ref: str) -> Optional[str]:
        try:
            return _git(bare, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        except GitError:
            return None

    def _commit(self, bare: Path, sha: str, full: str) -> dict[str, Any]:
        raw = _git(bare, "cat-file", "commit", sha)
        tree = re.search(r"^tree (\w+)", raw, re.M).group(1)  # type: ignore[union-attr]
        parents = re.findall(r"^parent (\w+)", raw, re.M)
        message = raw.split("\n\n", 1)[1] if "\n\n" in raw else ""
        return {"sha": sha, "node_id": sha, "url": f"https://api.github.com/repos/{full}/git/commits/{sha}",
                "html_url": f"https://github.com/{full}/commit/{sha}",
                "tree": {"sha": tree}, "message": message,
                "parents": [{"sha": p} for p in parents],
                "commit": {"message": message, "tree": {"sha": tree}},
                "author": {"name": "boss-e2e"}, "committer": {"name": "boss-e2e"}}

    def _ref_payload(self, full: str, branch: str, sha: str) -> dict[str, Any]:
        return {"ref": f"refs/heads/{branch}", "node_id": branch,
                "url": f"https://api.github.com/repos/{full}/git/refs/heads/{branch}",
                "object": {"sha": sha, "type": "commit"}}

    def _files_between(self, bare: Path, base: str, head: str) -> list[dict[str, Any]]:
        out = _git(bare, "diff", "--numstat", f"{base}...{head}")
        status = dict(line.split("\t", 1)[::-1] for line in
                      _git(bare, "diff", "--name-status", f"{base}...{head}").splitlines() if "\t" in line)
        files = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add, rem, name = parts
            add_i = int(add) if add.isdigit() else 0
            rem_i = int(rem) if rem.isdigit() else 0
            letter = (status.get(name) or "M")[:1]
            files.append({"filename": name, "additions": add_i, "deletions": rem_i,
                          "changes": add_i + rem_i,
                          "status": {"A": "added", "D": "removed", "R": "renamed"}.get(letter, "modified"),
                          "patch": ""})
        return files

    def _pr(self, full: str, number: int) -> Optional[dict[str, Any]]:
        return next((p for p in self.world.prs if p["repo"] == full and p["number"] == number), None)

    def _pr_payload(self, bare: Path, full: str, pr: dict[str, Any]) -> dict[str, Any]:
        head_sha = self._sha_of(bare, f"refs/heads/{pr['head']}") or pr.get("head_sha") or _ZERO
        base_sha = self._sha_of(bare, f"refs/heads/{pr['base']}") or _ZERO
        n = pr["number"]
        return {"number": n, "id": n, "node_id": f"PR_{n}", "state": pr["state"],
                "merged": pr["merged"], "draft": False, "title": pr.get("title"),
                "body": pr.get("body"), "merge_commit_sha": pr.get("merge_sha"),
                "html_url": f"https://github.com/{full}/pull/{n}",
                "url": f"https://api.github.com/repos/{full}/pulls/{n}",
                "head": {"ref": pr["head"], "sha": head_sha, "label": f"{full.split('/')[0]}:{pr['head']}",
                         "repo": {"full_name": full}},
                "base": {"ref": pr["base"], "sha": base_sha, "repo": {"full_name": full}},
                "mergeable": not pr["merged"], "mergeable_state": "clean",
                "user": {"login": "boss-e2e"}, "created_at": pr.get("created_at")}

    def _write_tree(self, bare: Path, base_tree: Optional[str], entries: list[dict[str, Any]]) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            if base_tree:
                _git(bare, "read-tree", base_tree, env=env)
            else:
                _git(bare, "read-tree", "--empty", env=env)
            for entry in entries:
                path = str(entry.get("path") or "")
                if not path:
                    continue
                if entry.get("sha") is None and "content" not in entry:
                    _git(bare, "update-index", "--force-remove", "--", path, env=env)
                    continue
                sha = entry.get("sha")
                if not sha:
                    sha = _git(bare, "hash-object", "-w", "--stdin",
                               input_bytes=str(entry.get("content") or "").encode())
                _git(bare, "update-index", "--add", "--cacheinfo",
                     f"{entry.get('mode') or '100644'},{sha},{path}", env=env)
            return _git(bare, "write-tree", env=env)

    def _new_commit(self, bare: Path, tree: str, parents: list[str], message: str) -> str:
        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        return _git(bare, *args, input_bytes=(message or "commit").encode())

    def _merge(self, bare: Path, base: str, head_sha: str, message: str) -> str:
        """Merge head into base: fast-forward when possible, else a real three-way merge."""
        base_sha = self._sha_of(bare, f"refs/heads/{base}")
        if not base_sha:
            _git(bare, "update-ref", f"refs/heads/{base}", head_sha)
            return head_sha
        if _git(bare, "merge-base", base_sha, head_sha) == base_sha:
            _git(bare, "update-ref", f"refs/heads/{base}", head_sha, base_sha)
            return head_sha
        tree = _git(bare, "merge-tree", "--write-tree", base_sha, head_sha).splitlines()[0]
        sha = self._new_commit(bare, tree, [base_sha, head_sha], message)
        _git(bare, "update-ref", f"refs/heads/{base}", sha, base_sha)
        return sha

    # ------------------------------------------------------------------ dispatch
    def handle(self, method: str, path: str, query: str, body: Any) -> tuple[int, Any]:
        if path in ("/", "/user"):
            return 200, {"login": "boss-e2e", "id": 1, "type": "User"}
        if path == "/rate_limit":
            return 200, {"resources": {"core": {"limit": 5000, "remaining": 4999, "used": 1,
                                                "reset": int(time.time()) + 3600}}}
        if path in ("/user/repos", "/installation/repositories"):
            return 200, []
        m = re.match(r"^/repos/([^/]+)/([^/]+)(/.*)?$", path)
        if not m:
            return 404, {"message": f"boss-e2e GitHub fake: no route for {method} {path}"}
        owner, repo, rest = m.group(1), m.group(2), m.group(3) or ""
        full = f"{owner}/{repo}"
        body = body if isinstance(body, dict) else {}
        with _LOCK:
            bare = self.remotes.ensure(owner, repo)
            try:
                return self._repo_route(bare, full, method, rest, query, body)
            except GitError as exc:
                return 422, {"message": f"boss-e2e GitHub fake: {exc}"}

    def _repo_route(self, bare: Path, full: str, method: str, rest: str, query: str,
                    body: dict[str, Any]) -> tuple[int, Any]:
        q = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        if rest == "":
            return 200, {"id": 1, "name": full.split("/")[1], "full_name": full, "private": True,
                         "default_branch": "main", "owner": {"login": full.split("/")[0]},
                         "permissions": {"admin": True, "push": True, "pull": True},
                         "html_url": f"https://github.com/{full}",
                         "clone_url": f"https://github.com/{full}.git"}
        m = re.match(r"^/git/ref/heads/(.+)$", rest) or re.match(r"^/git/refs/heads/(.+)$", rest)
        if m and method == "GET":
            sha = self._sha_of(bare, f"refs/heads/{m.group(1)}")
            if not sha:
                return 404, {"message": "Not Found"}
            return 200, self._ref_payload(full, m.group(1), sha)
        if m and method in ("PATCH", "POST"):
            old = self._sha_of(bare, f"refs/heads/{m.group(1)}")
            new = str(body.get("sha") or "")
            if old and not body.get("force") and _git(bare, "merge-base", old, new) != old:
                return 422, {"message": "Update is not a fast forward"}
            _git(bare, "update-ref", f"refs/heads/{m.group(1)}", new)
            return 200, self._ref_payload(full, m.group(1), new)
        if m and method == "DELETE":
            _git(bare, "update-ref", "-d", f"refs/heads/{m.group(1)}")
            return 204, ""
        if rest == "/git/refs" and method == "POST":
            ref = str(body.get("ref") or "")
            if self._sha_of(bare, ref):
                return 422, {"message": "Reference already exists"}
            _git(bare, "update-ref", ref, str(body.get("sha")))
            return 201, self._ref_payload(full, ref.replace("refs/heads/", ""), str(body.get("sha")))
        m = re.match(r"^/branches/(.+)$", rest)
        if m:
            branch = m.group(1).replace("%2F", "/")
            sha = self._sha_of(bare, f"refs/heads/{branch}")
            if not sha:
                return 404, {"message": "Branch not found"}
            return 200, {"name": branch, "protected": False, "protection": {},
                         "commit": self._commit(bare, sha, full)}
        m = re.match(r"^/git/commits/(\w+)$", rest)
        if m:
            return 200, self._commit(bare, m.group(1), full)
        if rest == "/git/commits" and method == "POST":
            sha = self._new_commit(bare, str(body.get("tree")),
                                   [str(p) for p in body.get("parents") or []],
                                   str(body.get("message") or ""))
            return 201, self._commit(bare, sha, full)
        if rest == "/git/blobs" and method == "POST":
            data = body.get("content") or ""
            raw = (base64.b64decode(data) if body.get("encoding") == "base64"
                   else str(data).encode())
            sha = _git(bare, "hash-object", "-w", "--stdin", input_bytes=raw)
            return 201, {"sha": sha, "url": f"https://api.github.com/repos/{full}/git/blobs/{sha}"}
        if rest == "/git/trees" and method == "POST":
            sha = self._write_tree(bare, body.get("base_tree"), list(body.get("tree") or []))
            return 201, {"sha": sha, "tree": []}
        m = re.match(r"^/commits/([^/]+)(/status|/check-runs|/statuses)?$", rest)
        if m:
            if m.group(2) == "/status":
                return 200, {"state": "success", "statuses": [], "total_count": 0}
            if m.group(2) in ("/check-runs",):
                runs = self._check_runs(bare, full, m.group(1))
                return 200, {"total_count": len(runs), "check_runs": runs}
            if m.group(2) == "/statuses":
                return 200, []
            sha = self._sha_of(bare, m.group(1)) or self._sha_of(bare, f"refs/heads/{m.group(1)}")
            if not sha:
                return 404, {"message": "No commit found"}
            return 200, self._commit(bare, sha, full)
        m = re.match(r"^/compare/(.+?)\.\.\.(.+)$", rest)
        if m:
            base = self._sha_of(bare, m.group(1)) or self._sha_of(bare, f"refs/heads/{m.group(1)}")
            head = self._sha_of(bare, m.group(2)) or self._sha_of(bare, f"refs/heads/{m.group(2)}")
            if not (base and head):
                return 404, {"message": "Not Found"}
            mb = _git(bare, "merge-base", base, head)
            ahead = int(_git(bare, "rev-list", "--count", f"{mb}..{head}"))
            behind = int(_git(bare, "rev-list", "--count", f"{mb}..{base}"))
            status = ("identical" if ahead == behind == 0 else "ahead" if behind == 0
                      else "behind" if ahead == 0 else "diverged")
            return 200, {"status": status, "ahead_by": ahead, "behind_by": behind,
                         "total_commits": ahead, "merge_base_commit": self._commit(bare, mb, full),
                         "files": self._files_between(bare, base, head),
                         "commits": [self._commit(bare, s, full) for s in
                                     _git(bare, "rev-list", f"{mb}..{head}").split()]}
        m = re.match(r"^/contents/(.+)$", rest)
        if m:
            ref = q.get("ref") or str(body.get("branch") or "main")
            if method == "GET":
                try:
                    blob = _git(bare, "rev-parse", f"{ref}:{m.group(1)}")
                except GitError:
                    return 404, {"message": "Not Found"}
                data = subprocess.run(["git", "--git-dir", str(bare), "cat-file", "blob", blob],
                                      capture_output=True).stdout
                return 200, {"type": "file", "path": m.group(1), "sha": blob,
                             "encoding": "base64", "content": base64.b64encode(data).decode()}
            if method == "PUT":
                branch = str(body.get("branch") or "main")
                parent = self._sha_of(bare, f"refs/heads/{branch}")
                base_tree = self._commit(bare, parent, full)["tree"]["sha"] if parent else None
                raw = base64.b64decode(body.get("content") or "")
                blob = _git(bare, "hash-object", "-w", "--stdin", input_bytes=raw)
                tree = self._write_tree(bare, base_tree, [{"path": m.group(1), "sha": blob}])
                sha = self._new_commit(bare, tree, [parent] if parent else [],
                                       str(body.get("message") or "update"))
                _git(bare, "update-ref", f"refs/heads/{branch}", sha)
                return 201, {"content": {"sha": blob, "path": m.group(1)},
                             "commit": self._commit(bare, sha, full)}
        if rest == "/merges" and method == "POST":
            head = self._sha_of(bare, str(body.get("head"))) or self._sha_of(
                bare, f"refs/heads/{body.get('head')}")
            if not head:
                return 404, {"message": "head not found"}
            sha = self._merge(bare, str(body.get("base")), head,
                              str(body.get("commit_message") or "merge"))
            return 201, self._commit(bare, sha, full)
        if rest.startswith("/pulls"):
            return self._pulls(bare, full, method, rest, q, body)
        m = re.match(r"^/issues/(\d+)/(comments|labels)$", rest)
        if m:
            bucket = "pr_comments" if m.group(2) == "comments" else "pr_labels"
            if method == "POST":
                entry = {"repo": full, "issue": int(m.group(1)),
                         **({"body": str(body.get("body") or "")[:8000]} if bucket == "pr_comments"
                            else {"labels": [str(x.get("name") if isinstance(x, dict) else x)
                                             for x in (body.get("labels") or [])]})}
                self.world.add(bucket, entry)
                if bucket == "pr_labels":
                    return 200, [{"name": n} for n in entry["labels"]]
                return 201, {"id": len(self.world.pr_comments), "body": entry["body"]}
            items = [e for e in getattr(self.world, bucket)
                     if e["repo"] == full and e["issue"] == int(m.group(1))]
            if bucket == "pr_labels":
                return 200, [{"name": n} for e in items for n in e["labels"]]
            return 200, [{"id": i + 1, "body": e["body"]} for i, e in enumerate(items)]
        if rest.startswith("/issues"):
            return (201, {"id": 1, "number": 1}) if method == "POST" else (200, [])
        if rest == "/deployments":
            return (201, {"id": len(self.world.deploys) + 1}) if method == "POST" else (200, [])
        if re.match(r"^/deployments/\d+/statuses$", rest):
            return (201, {"id": 1, "state": body.get("state")}) if method == "POST" else (200, [])
        if rest.startswith(("/hooks", "/keys", "/actions", "/collaborators", "/labels")):
            return (201, {"id": 1}) if method == "POST" else (200, [])
        return 404, {"message": f"boss-e2e GitHub fake: no route for {method} {rest}"}

    def _pulls(self, bare: Path, full: str, method: str, rest: str, q: dict[str, str],
               body: dict[str, Any]) -> tuple[int, Any]:
        if rest == "/pulls" and method == "POST":
            head = str(body.get("head") or "").split(":")[-1]
            if not self._sha_of(bare, f"refs/heads/{head}"):
                return 422, {"message": f"Validation Failed: head {head} does not exist"}
            existing = next((p for p in self.world.prs if p["repo"] == full and p["head"] == head
                             and p["state"] == "open"), None)
            if existing:
                return 422, {"message": "A pull request already exists for " + head}
            pr = self.world.add("prs", {"repo": full, "title": body.get("title"), "head": head,
                                        "base": str(body.get("base") or "main"),
                                        "body": str(body.get("body") or "")[:4000],
                                        "state": "open", "merged": False,
                                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")})
            return 201, self._pr_payload(bare, full, pr)
        if rest == "/pulls":
            head = q.get("head", "").split(":")[-1].replace("%2F", "/")
            state = q.get("state", "open")
            items = [p for p in self.world.prs if p["repo"] == full
                     and (state == "all" or p["state"] == state)
                     and (not head or p["head"] == head)]
            return 200, [self._pr_payload(bare, full, p) for p in items]
        m = re.match(r"^/pulls/(\d+)(/[a-z-]+)?$", rest)
        if not m:
            return 404, {"message": "Not Found"}
        pr = self._pr(full, int(m.group(1)))
        if pr is None:
            return 404, {"message": "Not Found"}
        sub = m.group(2) or ""
        if sub == "/merge" and method == "PUT":
            if pr["merged"]:
                return 405, {"message": "Pull Request is not mergeable"}
            head = self._sha_of(bare, f"refs/heads/{pr['head']}")
            if body.get("sha") and str(body["sha"]) != head:
                # GitHub's required-SHA merge: the head moved since the caller looked
                return 409, {"message": "Head branch was modified. Review and try the merge again."}
            pr["merged_at_sha"] = head
            sha = self._merge(bare, pr["base"], head, str(body.get("commit_title") or pr.get("title") or "merge"))
            pr.update(merged=True, state="closed", merge_sha=sha, merged_at=time.time())
            return 200, {"merged": True, "sha": sha, "message": "Pull Request successfully merged"}
        if sub == "/merge":
            return (204, "") if pr["merged"] else (404, {"message": "Not merged"})
        if sub == "/update-branch":
            return 202, {"message": "Updating pull request branch."}
        if sub == "/files":
            return 200, self._files_between(bare, f"refs/heads/{pr['base']}", f"refs/heads/{pr['head']}")
        if sub in ("/reviews", "/comments", "/requested_reviewers"):
            return (200, {"id": 1, "state": body.get("event") or "COMMENTED"}) if method == "POST" else (200, [])
        if method == "PATCH":
            for key in ("title", "body", "state"):
                if key in body:
                    pr[key] = body[key]
        return 200, self._pr_payload(bare, full, pr)

