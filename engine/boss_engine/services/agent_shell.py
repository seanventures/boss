"""A shell for the console agent.

WHY THIS EXISTS
---------------
The agent had twelve hand-written tools and still could not answer most questions, because
every new question needed a new tool: `read_repo_file` answers "what is in this file" and
nothing else. Twenty tools would not have fixed it. What actually answers open-ended questions
is a shell — grep across a tree, diff two files, run the app's own checks, read a log — the
same substrate a person uses.

BOSS already runs this substrate for BUILDS: the coding agent is claude-code in a sandbox with
the repo checked out. This gives the console surface the same thing, with a narrower write
scope: read broadly, act only through the governed tools that already have approval gates.

WHAT IS AND IS NOT GUARDED
--------------------------
The workspace is a THROWAWAY shallow clone. Anything the agent does to it is cheap and local,
so the guard does not try to prevent editing, deleting or rewriting files there — that is the
point of having a shell, and a guard that blocks ordinary work teaches people to avoid the
tool.

What it does refuse is the small set of actions that reach OUTSIDE the workspace or are
irreversible: publishing (`git push`, `gh pr`), privilege escalation, host-level destruction,
and reading the credential the clone was made with. Those are refused by name, and the refusal
is honest about its limits — a determined model can obfuscate a string. The real containment is
that this runs on a disposable checkout with a tokenless remote, not that the parser is clever.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

WORKSPACE_ROOT = Path(os.getenv("BOSS_AGENT_WORKSPACE_ROOT", "/tmp/boss-agent-ws"))
COMMAND_TIMEOUT_SECONDS = int(os.getenv("BOSS_AGENT_SHELL_TIMEOUT", "90"))
OUTPUT_CHAR_CAP = 20_000

# Refused by name. Each is either irreversible or reaches beyond the throwaway checkout.
_REFUSED = (
    (r"\bgit\s+push\b", "git push — publishing goes through BOSS's governed tools, not the shell"),
    (r"\bgh\s+(pr|release|repo\s+delete)\b", "gh publishing — use create_followup_task / change_existing_app"),
    (r"\bsudo\b", "sudo — the agent runs unprivileged"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "host power control"),
    (r"\bmkfs\b|\bdd\s+if=", "raw disk operations"),
    (r"\brm\s+(-[a-zA-Z]*\s+)*/(\s|$)", "rm on the filesystem root"),
    (r":\(\)\s*\{.*\};\s*:", "fork bomb"),
    (r"\bgit\s+remote\s+(set-url|add)\b", "changing the remote — the workspace is deliberately tokenless"),
)


def workspace_for(run_id: str) -> Path:
    return WORKSPACE_ROOT / re.sub(r"[^A-Za-z0-9_.-]", "_", run_id or "general")


def shell_rejection(command: str) -> Optional[str]:
    """Why this command must not run, or None."""
    text = (command or "").strip()
    if not text:
        return "empty command"
    lowered = text.lower()
    for pattern, reason in _REFUSED:
        if re.search(pattern, lowered):
            return f"refused: {reason}"
    return None


def ensure_workspace(
    run_id: str,
    *,
    owner: str,
    repo: str,
    branch: str,
    clone_url: str,
    force: bool = False,
) -> tuple[Optional[Path], Optional[str]]:
    """A shallow checkout of the app's branch, reused across turns.

    The clone URL carries a token, and `.git/config` would keep it where `cat` can reach it —
    a shell that can read its own credential is a shell that can use it elsewhere. So the
    remote is rewritten to a tokenless URL immediately after cloning; the checkout stays
    readable and stops being publishable.
    """
    path = workspace_for(run_id)
    if force and path.exists():
        shutil.rmtree(path, ignore_errors=True)
    if (path / ".git").is_dir():
        return path, None
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(path)],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-300:]
        # Never echo the clone URL back: it contains the token.
        return None, f"could not check out {owner}/{repo}@{branch}: {_scrub(detail)}"
    subprocess.run(
        ["git", "-C", str(path), "remote", "set-url", "origin",
         f"https://github.com/{owner}/{repo}.git"],
        capture_output=True, text=True, timeout=30,
    )
    return path, None


def _scrub(text: str) -> str:
    """Strip anything that looks like a credential out of output before it is returned."""
    text = re.sub(r"(https?://)[^/@\s]*@", r"\1", text)                 # user:token@host
    text = re.sub(r"\b(gh[pousr]_[A-Za-z0-9]{16,})\b", "[redacted]", text)
    text = re.sub(r"\b(sk-[A-Za-z0-9\-_]{16,})\b", "[redacted]", text)
    return text


def run_in_workspace(workspace: Path, command: str, cwd: Optional[str] = None) -> dict:
    """Run one command in the workspace and return what happened, including failures.

    A non-zero exit is a RESULT, not an error: "npm test fails with X" is the answer to the
    question that was asked. Only the harness failing to run it at all is an error.
    """
    rejection = shell_rejection(command)
    if rejection:
        return {"error": rejection, "exit_code": None}

    target = workspace
    if cwd:
        candidate = (workspace / cwd).resolve()
        # Confine to the workspace: `cd ../../etc` is not a diagnosis.
        if not str(candidate).startswith(str(workspace.resolve())):
            return {"error": "cwd must stay inside the workspace", "exit_code": None}
        target = candidate
    if not target.is_dir():
        return {"error": f"no such directory in the workspace: {cwd}", "exit_code": None}

    # A clean environment: the child gets no BOSS credentials to find.
    env = {
        k: v for k, v in os.environ.items()
        if not re.search(r"(TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL)", k, re.I)
    }
    env["HOME"] = str(workspace)
    env["GIT_TERMINAL_PROMPT"] = "0"          # never block waiting for credentials

    try:
        proc = subprocess.run(
            ["sh", "-lc", command],
            cwd=str(target), env=env, capture_output=True, text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "error": f"timed out after {COMMAND_TIMEOUT_SECONDS}s",
            "exit_code": None,
            "hint": "narrow the command, or run the slow part with a smaller scope",
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "exit_code": None}

    def cap(text: str) -> tuple[str, bool]:
        clean = _scrub(text or "")
        if len(clean) <= OUTPUT_CHAR_CAP:
            return clean, False
        # Keep the END: errors and summaries live at the bottom of most output.
        return clean[-OUTPUT_CHAR_CAP:], True

    stdout, out_cut = cap(proc.stdout)
    stderr, err_cut = cap(proc.stderr)
    return {
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        # Declared, never silent: a truncated result the model reads as complete is worse
        # than a short one.
        "truncated": out_cut or err_cut,
        "cwd": str(target.relative_to(workspace)) or ".",
    }
