#!/usr/bin/env python3
"""Periodic real-Ollama release-gate smoke runner (BOSSAPP-1024).

Runs one small local Ollama model across two pinned harness-bench tasks in both `bare` and
`delegate` levels, recording results into the bench database. Opt-in / periodic release-gate
item — never downloads models in CI.

Usage:
    scripts/ollama-smoke.sh [--model qwen2.5-coder:7b] [--tasks helpdesk-classify-case,textkit-word-count]
                            [--db-url postgresql+psycopg://.../boss_test_bench] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Sequence
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "qwen2.5-coder:7b"
DEFAULT_TASKS = ("helpdesk-classify-case", "textkit-word-count")
DEFAULT_LEVELS = ("bare", "delegate")


def check_ollama_ready(base_url: str, model: str, *, timeout_s: float = 5.0) -> tuple[bool, list[str], str]:
    """Verify the Ollama OpenAI-compatible server answers `/models` and serves `model`."""
    endpoint = base_url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(endpoint, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return False, [], f"Ollama endpoint {endpoint} is unreachable: {exc}"
    available = [
        str(item.get("id") or item.get("name") or "")
        for item in (payload.get("data") or payload.get("models") or [])
        if isinstance(item, dict) and (item.get("id") or item.get("name"))
    ]
    if model and available and model not in available:
        return (
            False,
            available,
            f"model {model!r} is not pulled on {base_url} (available: {', '.join(sorted(available)[:8])})",
        )
    return True, available, f"ok ({len(available)} model(s) on {base_url})"


def build_smoke_commands(
    *,
    model: str = DEFAULT_MODEL,
    tasks: Sequence[str] = DEFAULT_TASKS,
    levels: Sequence[str] = DEFAULT_LEVELS,
    db_url: str = "",
    budget_usd: float = 1.0,
    allow_unisolated: bool = False,
    dry_run: bool = False,
) -> list[list[str]]:
    """Build the exact `scripts/harness_bench.py run` invocations for bare + delegate."""
    task_arg = ",".join(t.strip() for t in tasks if t.strip())
    engine_spec = f"ollama:{model}" if ":" not in model or not model.startswith("ollama:") else model
    commands: list[list[str]] = []
    for level in levels:
        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "harness_bench.py"),
            "run",
            "--engine",
            engine_spec,
            "--level",
            level,
            "--tasks",
            task_arg,
            "--budget-usd",
            str(budget_usd),
        ]
        if db_url:
            cmd += ["--db-url", db_url]
        if allow_unisolated:
            cmd.append("--allow-unisolated")
        if dry_run:
            cmd.append("--dry-run")
        commands.append(cmd)
    return commands


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Periodic real-Ollama smoke for BOSS release gate.")
    parser.add_argument("--model", default=os.environ.get("BOSS_OLLAMA_SMOKE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"))
    parser.add_argument("--tasks", default=",".join(DEFAULT_TASKS))
    parser.add_argument("--levels", default=",".join(DEFAULT_LEVELS))
    parser.add_argument("--db-url", default=os.environ.get("BOSS_BENCH_DATABASE_URL", ""))
    parser.add_argument("--budget-usd", type=float, default=1.0)
    parser.add_argument("--allow-unisolated", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    levels = [lv.strip() for lv in args.levels.split(",") if lv.strip()]
    commands = build_smoke_commands(
        model=args.model,
        tasks=tasks,
        levels=levels,
        db_url=args.db_url,
        budget_usd=args.budget_usd,
        allow_unisolated=args.allow_unisolated,
        dry_run=args.dry_run,
    )
    ok, _avail, detail = check_ollama_ready(args.base_url, args.model)
    print(f"[ollama-smoke] preflight ({args.model} @ {args.base_url}): {detail}")
    if args.dry_run:
        for cmd in commands:
            print("[ollama-smoke] dry-run:", " ".join(cmd))
        return 0 if ok else 2
    if not ok:
        print(f"[ollama-smoke] FAIL: {detail}", file=sys.stderr)
        return 2

    env = {
        **os.environ,
        "OLLAMA_BASE_URL": args.base_url,
        "OLLAMA_API_KEY": os.environ.get("OLLAMA_API_KEY", "ollama"),
        "OPENAI_BASE_URL": args.base_url,
        "OPENAI_API_KEY": os.environ.get("OLLAMA_API_KEY", "ollama"),
    }
    for cmd in commands:
        print("[ollama-smoke] running:", " ".join(cmd), flush=True)
        res = subprocess.run(cmd, cwd=ROOT, env=env, check=False)
        if res.returncode != 0:
            print(f"[ollama-smoke] FAIL (exit {res.returncode})", file=sys.stderr)
            return res.returncode
    print("[ollama-smoke] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
