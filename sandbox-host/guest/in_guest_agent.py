#!/usr/bin/env python3
"""In-guest agent — runs INSIDE the sandbox (a Docker container today as the
stand-in; a Kata/Firecracker microVM later). It is the only place source code
exists.

Protocol: reads a task spec JSON (BOSS_SPEC_PATH), runs the pipeline, and writes
a result JSON (BOSS_RESULT_PATH). Only governance metadata + the diff cross back
to the host over the agent channel (a mounted file today; vsock under Firecracker).

Engines:
  echo        — deterministic stand-in (writes a marker file) to prove the
                round-trip without an LLM or credentials.
  claude_code — wraps the `claude` CLI with its editing tools (the real agent).
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import urllib.request
from pathlib import Path

SPEC = Path(os.environ.get("BOSS_SPEC_PATH", "/work/spec.json"))
RESULT = Path(os.environ.get("BOSS_RESULT_PATH", "/work/result.json"))
WORK = Path(os.environ.get("BOSS_WORKDIR", "/work/repo"))

ARTIFACT_IGNORES = "__pycache__/\n*.pyc\ntarget/\n.gradle/\nnode_modules/\n.pytest_cache/\n"


def _run(cmd, cwd=None, timeout=900):
    argv = shlex.split(cmd) if isinstance(cmd, str) else cmd
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _git(args, cwd):
    return _run(["git", "-C", str(cwd), *args])


def _prepare_repo(spec):
    WORK.parent.mkdir(parents=True, exist_ok=True)
    if spec.get("test_mode"):
        # No GitHub: init a throwaway repo so the round-trip is testable offline.
        WORK.mkdir(parents=True, exist_ok=True)
        _git(["init", "-q"], WORK)
        (WORK / "README.md").write_text("sandbox test repo\n", encoding="utf-8")
        _git(["add", "-A"], WORK)
        _git(["-c", "user.name=seed", "-c", "user.email=s@s", "commit", "-q", "-m", "seed"], WORK)
    else:
        owner, repo = spec["owner"], spec["repo"]
        branch = spec.get("base_branch", "main")
        token = spec.get("token")
        url = (
            f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
            if token else f"https://github.com/{owner}/{repo}.git"
        )
        rc, _o, e = _run(["git", "clone", "--depth", "1", "--branch", branch, url, str(WORK)])
        if rc != 0:
            raise RuntimeError("clone failed: " + (e or "").replace(token or "\0", "***")[:300])
    _git(["config", "user.name", "boss-agent"], WORK)
    _git(["config", "user.email", "boss-agent@users.noreply.github.com"], WORK)
    try:
        (WORK / ".git" / "info" / "exclude").write_text(ARTIFACT_IGNORES, encoding="utf-8")
    except OSError:
        pass


SKIP_DIRS = {".git", "target", "node_modules", ".gradle", "build", "dist", "__pycache__", ".venv"}


def _repo_tree(limit=200):
    out = []
    for p in sorted(WORK.rglob("*")):
        rel = p.relative_to(WORK)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.is_file():
            out.append(str(rel))
        if len(out) >= limit:
            break
    return out


def _read_small_files(paths, budget=24000):
    blobs, used = [], 0
    for rel in paths:
        fp = WORK / rel
        try:
            txt = fp.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if len(txt) > 6000 or used + len(txt) > budget:
            continue
        blobs.append(f"=== {rel} ===\n{txt}")
        used += len(txt)
    return "\n\n".join(blobs)


def _chat(spec, messages, timeout=180):
    """Chat-completion call — Azure OpenAI if configured (endpoint+deployment),
    else the OpenAI platform. Same JSON contract either way."""
    az_ep, az_dep = spec.get("azure_endpoint"), spec.get("azure_deployment")
    if az_ep and az_dep:
        ver = spec.get("azure_api_version") or "2024-10-21"
        url = f"{az_ep.rstrip('/')}/openai/deployments/{az_dep}/chat/completions?api-version={ver}"
        headers = {"api-key": spec.get("azure_key") or spec.get("openai_api_key"),
                   "Content-Type": "application/json"}
        body = {"messages": messages, "response_format": {"type": "json_object"}, "temperature": 0}
    else:
        # Any OpenAI-COMPATIBLE provider via a configurable base URL: OpenAI (default),
        # xAI/Grok (https://api.x.ai/v1), Together, OpenRouter, a local server, etc.
        # Swapping model/provider is config only (openai_base_url + openai_api_key + model) —
        # no image rebuild, since they're all just this HTTP contract.
        base = (spec.get("openai_base_url") or os.environ.get("OPENAI_BASE_URL")
                or "https://api.openai.com/v1").rstrip("/")
        key = spec.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {"model": spec.get("model") or "gpt-4o", "messages": messages,
                "response_format": {"type": "json_object"}, "temperature": 0}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"], data.get("usage", {})


def _run_native_openai(spec) -> str:
    """Native code agent on the raw OpenAI API (architecture §3 — no CLI/keychain
    dependency, works headless in a sandbox). Asks the model for whole-file writes
    as JSON, applies them, runs the tests, and iterates with the failures fed back."""
    has_creds = (spec.get("azure_endpoint") and spec.get("azure_deployment")) or \
        spec.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")
    if not has_creds:
        raise RuntimeError("native engine needs Azure OpenAI or OpenAI credentials")
    criteria = "\n".join(f"- {c}" for c in spec.get("acceptance_criteria", [])) or "- (none)"
    tests = "\n".join(f"- {c}" for c in spec.get("test_commands", [])) or "- (none)"
    system = (
        "You are a senior engineer editing a real git repository. You implement the task by "
        "writing complete file contents. Reply ONLY as JSON: "
        '{"summary": str, "files": [{"path": str, "content": str}], "done": bool}. '
        "Each file is written in full (not a patch). Keep changes minimal and idiomatic; make the tests pass."
    )
    last_feedback = ""
    summary = ""
    for round_i in range(int(spec.get("max_rounds", 4))):
        tree = _repo_tree()
        ctx = _read_small_files([p for p in tree if any(
            p.endswith(x) for x in (".xml", ".gradle", ".java", ".py", ".js", ".ts", ".go", ".mod", ".json", ".md")
        )])
        user = (
            f"TASK: {spec.get('title','')}\n\n{spec.get('instructions','')}\n\n"
            f"ACCEPTANCE CRITERIA:\n{criteria}\n\nTEST COMMANDS (must pass):\n{tests}\n\n"
            f"REPO FILES:\n{chr(10).join(tree)}\n\nRELEVANT FILE CONTENTS:\n{ctx}\n"
            + (f"\nThe previous attempt's tests FAILED:\n{last_feedback}\nFix it." if last_feedback else "")
        )
        content, _usage = _chat(spec, [
            {"role": "system", "content": system}, {"role": "user", "content": user},
        ])
        try:
            data = json.loads(content)
        except Exception:
            data = {"summary": content[:300], "files": []}
        for f in data.get("files", []):
            path = (f.get("path") or "").lstrip("/")
            if not path or ".." in path:
                continue
            fp = WORK / path
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(f.get("content", ""), encoding="utf-8")
        summary = data.get("summary") or summary
        tres, passed = _run_tests(spec)
        if passed or (data.get("done") and not spec.get("test_commands")):
            return summary
        # feed the ACTUAL build/test errors back so the model can fix them
        last_feedback = "\n\n".join(
            f"$ {t['command']}  (exit {t['exit_code']})\n{t.get('output', '')[-1800:]}"
            for t in tres if t["exit_code"] != 0
        )[:6000]
    return summary


def _run_agent(spec) -> str:
    engine = spec.get("engine", "echo")
    if engine == "echo":
        (WORK / "SANDBOX_OK.txt").write_text(
            f"in-guest agent executed: {spec.get('title', '')}\n", encoding="utf-8"
        )
        return f"echo engine wrote SANDBOX_OK.txt for: {spec.get('title', '')}"
    if engine in ("openai", "native"):
        return _run_native_openai(spec)
    if engine == "claude_code":
        if spec.get("claude_oauth_token"):
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = spec["claude_oauth_token"]
        instructions = spec.get("instructions", "")
        criteria = "\n".join(f"- {c}" for c in spec.get("acceptance_criteria", [])) or "- (none)"
        tests = "\n".join(f"- {c}" for c in spec.get("test_commands", [])) or "- (none)"
        prompt = (
            "You are a senior engineer in a checked-out git repo (your cwd). Implement the "
            f"task by editing files, then make the tests pass.\n\nTASK: {spec.get('title','')}\n\n"
            f"{instructions}\n\nACCEPTANCE CRITERIA:\n{criteria}\n\nTEST/BUILD COMMANDS:\n{tests}\n\n"
            "Edit files in place; do NOT commit or push (the host handles git/PR). When done, "
            "summarize what you changed and why."
        )
        rc, o, e = _run(
            ["claude", "-p", prompt, "--output-format", "json",
             "--max-turns", str(spec.get("max_turns", 40)),
             "--allowedTools", spec.get("allowed_tools", "Read,Edit,MultiEdit,Write,Glob,Grep,Bash,TodoWrite"),
             "--permission-mode", "acceptEdits"],
            cwd=WORK, timeout=spec.get("timeout", 900),
        )
        if rc != 0:
            raise RuntimeError("claude agent failed: " + (e or o or "")[:300])
        try:
            return (json.loads(o).get("result") or "").strip()
        except Exception:
            return o[:400]
    raise RuntimeError(f"unknown engine: {engine}")


def _run_tests(spec):
    results, passed = [], True
    for cmd in spec.get("test_commands", []):
        t = time.monotonic()
        out = ""
        try:
            rc, o, e = _run(cmd, cwd=WORK, timeout=spec.get("timeout", 900))
            out = ((o or "") + "\n" + (e or "")).strip()
        except subprocess.TimeoutExpired:
            rc, out = None, "timed out"
        results.append({"command": cmd, "exit_code": rc,
                        "duration_ms": int((time.monotonic() - t) * 1000),
                        "output": out[-3000:]})  # kept for the native agent's fix loop
        if rc != 0:
            passed = False
    return results, (passed if spec.get("test_commands") else False)


def _diff():
    _git(["add", "-A"], WORK)
    rc, numstat, _e = _git(["diff", "--cached", "--numstat"], WORK)
    files, adds, dels, paths = [], 0, 0, []
    for line in (numstat or "").strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, d, p = parts
        adds += 0 if a == "-" else int(a)
        dels += 0 if d == "-" else int(d)
        paths.append(p)
        fp = WORK / p
        if fp.is_file():
            try:
                files.append({"path": p, "content": fp.read_text(encoding="utf-8")})
            except (UnicodeDecodeError, OSError):
                pass
    return files, {"files_changed": len(paths), "additions": adds, "deletions": dels, "paths": paths}


def main():
    spec = json.loads(SPEC.read_text())
    try:
        _prepare_repo(spec)
        summary = _run_agent(spec)
        tests, passed = _run_tests(spec)
        files, dsum = _diff()
        out = {
            "status": "ok", "engine": spec.get("engine", "echo"), "summary": summary,
            "diff": files, "diff_summary": dsum, "tests": tests, "tests_passed": passed,
        }
    except Exception as ex:  # noqa: BLE001
        out = {"status": "error", "error": str(ex)}
    RESULT.write_text(json.dumps(out))
    # stdout log NEVER includes source (the diff) — only metadata
    print(json.dumps({k: v for k, v in out.items() if k != "diff"}))


if __name__ == "__main__":
    main()
