"""Fetch a customer template from git, read-only (F2 §2.5 step 1, §2.9 S2.5).

The import is a supply-chain boundary. What this module guarantees:

  * the ONLY program it runs is `git` (`_git`, the one subprocess call; a test pins it), and git
    never checks anything out: the commit is fetched into a BARE quarantine repository and read
    from its objects (`ls-tree`, `cat-file --batch`), so no file of the template is ever written
    as a file, a symlink is never created, and nothing in it can run;
  * hooks are off (`core.hooksPath=/dev/null`, and a bare fetch runs none anyway), submodules are
    never fetched, objects are fsck'd, git's system and global config are ignored, no credential
    prompt can appear;
  * the URL is https only, to a public hostname (no IP literals, no local names, no user-info);
    `file://` is allowed only when the install says so (`allow_local`, tests and air-gapped
    mirrors);
  * the fetch goes through boss-egress's CONNECT proxy when the install has one
    (`BOSS_EGRESS_CONNECT_PROXY`): the template's host must be on the egress allowlist, which is
    the operator's decision, never the template's;
  * limits are enforced while reading: more than MAX_FILES entries, a file over MAX_FILE_BYTES or
    a total over MAX_TOTAL_BYTES is recorded and its bytes are never read; the quarantine
    repository itself may not exceed MAX_REPO_BYTES.

A ref resolves to a commit SHA here; the approval pins that SHA and the tree's digest (no
"latest").
"""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .validator import (
    MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES, TemplateTree, TreeEntry, safe_relpath,
)

MAX_REPO_BYTES = 64 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 120
_REF = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/\-]{1,200}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_LOCAL_TLDS = {"localhost", "local", "internal", "lan", "home", "corp", "test", "invalid"}


class ImportRefused(Exception):
    """The URL or ref is refused before anything is fetched, or the fetch failed. `reason` is
    one secret-free sentence."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Fetched:
    commit_sha: str
    tree: TemplateTree
    git_url: str


def check_url(git_url: str, *, allow_local: bool = False) -> str:
    """The URL, normalised, or ImportRefused. Never echoes user-info back."""
    url = (git_url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ImportRefused("git_url is not a URL") from None
    if parts.scheme == "file":
        if not allow_local:
            raise ImportRefused("git_url: file:// is not allowed on this install; use https://")
        if not parts.path or ".." in Path(parts.path).parts:
            raise ImportRefused("git_url: a file:// URL needs an absolute path")
        return f"file://{parts.path}"
    if parts.scheme != "https":
        raise ImportRefused("git_url must be https:// (no ssh, git://, http:// or ext::)")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ImportRefused("git_url must not carry a user name or password; "
                            "use a GitHub connection for a private repository")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ImportRefused("git_url has no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ImportRefused("git_url: an IP address is not allowed; use the repository's hostname")
    labels = host.split(".")
    if len(labels) < 2 or labels[-1] in _LOCAL_TLDS:
        if not allow_local:
            raise ImportRefused(f"git_url: {host} is not a public hostname")
    if parts.port not in (None, 443):
        raise ImportRefused("git_url: only the standard HTTPS port is allowed")
    if parts.query or parts.fragment:
        raise ImportRefused("git_url must not have a query or fragment")
    return f"https://{host}{parts.path}"


def check_ref(ref: Optional[str]) -> str:
    ref = (ref or "HEAD").strip()
    if not _REF.match(ref) or ref.endswith(".lock") or "@{" in ref:
        raise ImportRefused("ref must be a branch, tag or 40-character commit SHA")
    return ref


def _git_env(home: Path, *, allow_local: bool, proxy: Optional[str]) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_ALLOW_PROTOCOL": "https:file" if allow_local else "https",
        "LC_ALL": "C",
    }
    for key in ("SSL_CERT_FILE", "GIT_SSL_CAINFO", "REQUESTS_CA_BUNDLE"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if proxy:
        env["HTTPS_PROXY"] = env["https_proxy"] = proxy
    return env


#: Config on every git call: no hooks, no submodules, fsck what arrives, no credential helper,
#: no protocol but https (and file only when allowed).
_SAFE_CONFIG = ("core.hooksPath=/dev/null", "core.symlinks=false", "core.fsmonitor=false",
                "submodule.recurse=false", "fetch.recurseSubmodules=false",
                "transfer.fsckObjects=true", "fetch.fsckObjects=true", "credential.helper=",
                "protocol.allow=never", "protocol.https.allow=always", "http.followRedirects=false",
                "advice.detachedHead=false", "core.askPass=")


def _git(args: list[str], *, cwd: Path, env: dict[str, str], allow_local: bool,
         proxy: Optional[str], input_bytes: Optional[bytes] = None,
         timeout: int = FETCH_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """THE one subprocess this module runs. argv[0] is always git."""
    config: list[str] = []
    for item in _SAFE_CONFIG + (("protocol.file.allow=always",) if allow_local else ()) \
            + ((f"http.proxy={proxy}",) if proxy else ()):
        config += ["-c", item]
    return subprocess.run(["git", *config, *args], cwd=str(cwd), env=env, input=input_bytes,
                          capture_output=True, timeout=timeout, check=False)


def _du(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(dirpath) / f).lstat().st_size
            except OSError:
                pass
    return total


def _short_error(proc: subprocess.CompletedProcess) -> str:
    text = (proc.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()
    line = text[-1] if text else f"git exited {proc.returncode}"
    # never echo a URL's user-info back (there should be none; belt and braces)
    return re.sub(r"//[^/@\s]+@", "//", line)[:300]


def fetch(git_url: str, ref: Optional[str] = None, *, allow_local: bool = False,
          proxy: Optional[str] = None, workdir: Optional[Path] = None) -> Fetched:
    """Fetch `ref` of `git_url` into a bare quarantine repository and read its tree from git
    objects. Removes the quarantine directory before returning."""
    url = check_url(git_url, allow_local=allow_local)
    ref = check_ref(ref)
    proxy = proxy if proxy is not None else (os.environ.get("BOSS_EGRESS_CONNECT_PROXY") or None)
    base = Path(tempfile.mkdtemp(prefix="boss-template-import-", dir=workdir))
    try:
        repo = base / "q.git"
        home = base / "home"
        home.mkdir()
        env = _git_env(home, allow_local=allow_local, proxy=proxy)
        kw = {"env": env, "allow_local": allow_local, "proxy": proxy}
        init = _git(["init", "-q", "--bare", str(repo)], cwd=base, **kw)
        if init.returncode != 0:
            raise ImportRefused("could not create the quarantine repository")
        got = _git(["fetch", "-q", "--depth", "1", "--no-tags", "--no-recurse-submodules",
                    "--", url, ref], cwd=repo, **kw)
        if got.returncode != 0:
            raise ImportRefused(f"git fetch failed: {_short_error(got)}")
        if _du(repo) > MAX_REPO_BYTES:
            raise ImportRefused(f"the repository is larger than {MAX_REPO_BYTES // (1024 * 1024)} MB")
        head = _git(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], cwd=repo, **kw)
        sha = (head.stdout or b"").decode().strip()
        if head.returncode != 0 or not _SHA.match(sha):
            raise ImportRefused("the ref did not resolve to a commit")
        if _SHA.match(ref) and sha != ref:
            raise ImportRefused("the fetched commit is not the one asked for")
        tree = _read_tree(repo, sha, kw)
        return Fetched(commit_sha=sha, tree=tree, git_url=url)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _read_tree(repo: Path, sha: str, kw: dict) -> TemplateTree:
    listed = _git(["ls-tree", "-r", "-l", "-z", "--full-tree", sha], cwd=repo, **kw)
    if listed.returncode != 0:
        raise ImportRefused("could not list the commit's files")
    tree = TemplateTree()
    to_read: list[tuple[str, str]] = []
    total = 0
    records = [r for r in (listed.stdout or b"").split(b"\x00") if r]
    if len(records) > MAX_FILES:
        tree.problems.append(f"more than {MAX_FILES} files")
    for rec in records[:MAX_FILES + 1]:
        meta, _, raw_path = rec.partition(b"\t")
        path = raw_path.decode("utf-8", errors="replace")
        try:
            mode, otype, oid, size_s = meta.decode().split()
        except ValueError:
            tree.problems.append("could not parse the commit's file list")
            continue
        clean = safe_relpath(path)
        if clean is None:
            tree.problems.append(f"{path!r}: path escapes the template root")
            continue
        if mode == "120000":
            tree.add(TreeEntry(clean, "symlink"))
        elif mode == "160000" or otype == "commit":
            tree.add(TreeEntry(clean, "submodule"))
        elif otype != "blob" or mode not in ("100644", "100755"):
            tree.add(TreeEntry(clean, "other"))
        else:
            size = int(size_s) if size_s.isdigit() else 0
            tree.add(TreeEntry(clean, "file", size))
            total += size
            if size <= MAX_FILE_BYTES and total <= MAX_TOTAL_BYTES:
                to_read.append((clean, oid))
    if to_read:
        batch = _git(["cat-file", "--batch"], cwd=repo,
                     input_bytes=b"".join(oid.encode() + b"\n" for _, oid in to_read), **kw)
        if batch.returncode != 0:
            raise ImportRefused("could not read the commit's files")
        out = batch.stdout or b""
        pos = 0
        for clean, oid in to_read:
            nl = out.index(b"\n", pos)
            header = out[pos:nl].decode().split()
            if len(header) != 3 or header[0] != oid or header[1] != "blob":
                raise ImportRefused("the commit's files could not be read consistently")
            size = int(header[2])
            tree.entries[clean].data = out[nl + 1: nl + 1 + size]
            pos = nl + 1 + size + 1
    return tree
