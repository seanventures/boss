"""Identify the submitted files of an existing local change independently of the worktree diff."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

from . import local_repo


@dataclass
class SubmittedChange:
    paths: list[str]
    documents_only: bool


def inspect(git_dir: Path, base: str, head: str) -> SubmittedChange:
    # Commit IDs come from the stored change. No rename detection: a rename out of code
    # still includes the removed source path. Inspect both tree modes, including deletions.
    raw = local_repo.git(["diff", "--raw", "--no-renames", "-z", f"{base}...{head}", "--"],
                         git_dir=git_dir, raw=True)
    fields = raw.split(b"\0")[:-1]
    paths = []
    documents_only = bool(fields)
    for header, name in zip(fields[::2], fields[1::2], strict=True):
        old, new = header[1:].split()[:2]
        path = name.decode("utf-8", "replace")
        paths.append(path)
        documents_only = documents_only and prose_file(path, old) and prose_file(path, new)
    if documents_only:
        # A repository with any code, executable, symlink, manifest or test keeps its
        # normal build and test gates, even when this particular edit touches a document.
        tree = local_repo.git(["ls-tree", "-r", "-z", head], git_dir=git_dir, raw=True)
        for entry in tree.split(b"\0")[:-1]:
            meta, name = entry.split(b"\t", 1)
            mode = meta.split()[0]
            # The local-repo seed always includes a regular .gitignore. A change to this
            # file already failed the changed-path check above.
            if name == b".gitignore" and mode == b"100644":
                continue
            if not prose_file(name.decode("utf-8", "replace"), mode):
                documents_only = False
                break
    return SubmittedChange(paths, documents_only)


# FILES WHOSE WORDS ARE INSTRUCTIONS: a coding agent (BOSS's own included) reads these as its
# brief, so changing one changes what the next build does. They keep the full gates.
# Matched by STEM (the name before its first dot, any case), so the variants each engine also
# loads (CLAUDE.local.md, AGENTS.override.md, AGENT.md…) are the same file to this check.
_AGENT_INSTRUCTION_STEMS = frozenset({"agents", "agent", "claude", "gemini", "skill",
                                      "copilot-instructions", "conventions"})
_AGENT_INSTRUCTION_SUFFIXES = (".instructions.md", ".prompt.md")


def agent_instructions(name: str) -> bool:
    """A file a coding engine loads as its instructions (a lower-cased base name)."""
    return (name.split(".", 1)[0] in _AGENT_INSTRUCTION_STEMS
            or name.endswith(_AGENT_INSTRUCTION_SUFFIXES))
# .txt files a toolchain or platform reads as configuration, whatever directory they sit in.
_TOOL_READ_NAMES = frozenset({"cmakelists.txt", "runtime.txt", "apt.txt", "aptfile.txt",
                              "packages.txt", "pipfile.txt", "robots.txt"})


def prose_file(path: str, mode: bytes) -> bool:
    """A regular, non-executable document nothing runs or reads as configuration. Anything
    else (code, an executable bit, a symlink, a submodule, a hidden path such as CI or hooks,
    a dependency list, an agent's instructions) is not prose and keeps the full gates."""
    p = PurePosixPath(path)
    name = p.name.lower()
    parts = [part.lower() for part in p.parts]
    return (mode in {b"000000", b"100644"} and p.suffix.lower() in {".md", ".rst", ".txt"}
            and all(not component.startswith(".") for component in p.parts)
            and not agent_instructions(name) and name not in _TOOL_READ_NAMES
            # requirements.txt, dev-requirements.txt, requirements/dev.txt, constraints.txt…
            and not any("requirement" in part or "constraint" in part for part in parts))


def tree_is_the_reviewed_head(root: Path, head: str) -> bool:
    """The checkout is exactly the reviewed commit: same HEAD, nothing modified or added, an
    ignored file included, and no index entry told to look away from its file (skip-worktree or
    assume-unchanged hide an edit from `git status`). A documentation review that finds anything
    else is no longer one (the full gates run)."""
    try:
        at = local_repo.git(["rev-parse", "HEAD"], cwd=root).strip()
        dirty = local_repo.git(["status", "--porcelain", "--untracked-files=all", "--ignored"],
                               cwd=root)
        tags = local_repo.git(["ls-files", "-v", "-z"], cwd=root, raw=True)
    except local_repo.LocalRepoError:
        return False
    # `ls-files -v`: "S" is skip-worktree, a lower-case tag is assume-unchanged.
    hidden = any(entry[:1] == b"S" or entry[:1].islower()
                 for entry in tags.split(b"\0") if entry)
    return at == head and not dirty.strip() and not hidden


# MARKDOWN'S HARD LINE BREAK is two trailing spaces, so trailing whitespace is not a defect in
# a .md file. Every other check stays on for it (conflict markers, space before tab, a blank
# line at the end), and .rst/.txt keep all of them. BOSS's own attributes, never the tree's.
_DOCUMENT_WHITESPACE = "*.[mM][dD] whitespace=-blank-at-eol\n"


def check_documents(git_dir: Path, base: str, head: str) -> tuple[bool, str]:
    """`git diff --check` over the submitted range in BOSS's bare repository → (ok, words)."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".gitattributes", delete=False) as attrs:
        attrs.write(_DOCUMENT_WHITESPACE)
    args = ["-c", f"core.attributesFile={attrs.name}", "diff", "--check", "--no-ext-diff",
            f"{base}...{head}", "--"]
    try:
        # The problems are printed on stdout; the exit code alone would say only "exit 2".
        found = local_repo.git(args, git_dir=git_dir, check=False).strip()
        if found:
            return False, "git diff --check found:\n" + found[:2000]
        local_repo.git(args, git_dir=git_dir)
        return True, "Submitted documentation checked. No application test suite exists."
    except local_repo.LocalRepoError as exc:
        return False, str(exc)
    finally:
        Path(attrs.name).unlink(missing_ok=True)


class DocumentRouteBroken(RuntimeError):
    """The checkout changed after the document check had already answered for it."""


class DocumentRoute:
    """The documentation-only route of one review, re-proven at every use.

    `current()` is the document check while the checkout is still the reviewed head. The first
    time it is not, the route is revoked (`on_revoked` says so): before the check has answered
    anything, the full suite takes over; after it has, the run fails rather than carry a verdict
    about a tree that no longer exists."""

    def __init__(self, root: Path, head: str, check: Any,
                 on_revoked: Callable[[], None] = lambda: None):
        self.root, self.head, self._check, self._on_revoked = root, head, check, on_revoked
        self.answered = False
        self.broken = ""

    def current(self) -> Optional[Any]:
        if self.broken:
            # STAYS BROKEN: a caller that swallowed the first raise (the acceptance check reads
            # failures as "not run") must not let the next one fall back to the normal suite's
            # record for a tree the document check already answered for.
            raise DocumentRouteBroken(self.broken)
        if self._check is not None and not tree_is_the_reviewed_head(self.root, self.head):
            self._check = None
            self._on_revoked()
            if self.answered:
                self.broken = ("The checkout changed during a documentation review. Request a "
                               "review of the change's current head.")
                raise DocumentRouteBroken(self.broken)
        if self._check is not None:
            self.answered = True
        return self._check


REVIEW_CRITERIA = (
    "Check the submitted change. Build and run the existing suite when applicable. "
    "In a documentation-only repository report the document checks and the absence of an application suite.",
    "The review panel reviewed the change.",
)


def documentation_acceptance(items, *, stack, source, dropped, head, paths, check, verdict, votes, suspect):
    """Judge BOSS's own two review steps from this revision's Git and panel receipts."""
    from .acceptance.verdict import MET, MISSING, UNJUDGED, summarize_judgments
    from .review_input import hollow_reason

    if tuple(item["text"] for item in items) != REVIEW_CRITERIA or not paths or dropped:
        return None
    git_passed = check.exit_code == 0 and not check.timed_out
    git_evidence = (f"git diff --check on {head} exited {check.exit_code}. "
                    "The submitted tree contains documentation and has no application suite.")
    panel_status = UNJUDGED
    panel_evidence = "No verified review of the submitted files."
    if not suspect and verdict in ("survived", "refuted"):
        for result, vote in votes:
            summary = (result or {}).get("summary") or ""
            if vote == verdict and not hollow_reason(summary, paths):
                panel_status = MET if verdict == "survived" else MISSING
                panel_evidence = f"Panel verdict {verdict} on {head}. {summary[:700]}"
                break
    rows = [{**items[0], "status": MET if git_passed else MISSING, "evidence": git_evidence},
            {**items[1], "status": panel_status, "evidence": panel_evidence}]
    summary = "MET" if git_passed and panel_status == MET else "NOT MET"
    return summarize_judgments(items, rows, summary, stack, source)
