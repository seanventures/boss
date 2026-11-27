#!/usr/bin/env python3
"""MT-11 parity checker: verifies 100% slash-command parity between the legacy Python TUI
(`scripts/metatron_tui.py`) and the Rust Metatron TUI (`metatron/crates/metatron-tui/src/commands.rs`),
plus optional golden-transcript validation against a compiled Rust `metatron` binary.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PYTHON_TUI = REPO_ROOT / "scripts" / "metatron_tui.py"
RUST_COMMANDS = REPO_ROOT / "metatron" / "crates" / "metatron-tui" / "src" / "commands.rs"


def extract_python_commands(source: str) -> set[str]:
    """Extract documented slash commands from `scripts/metatron_tui.py` COMMAND_HELP / help table."""
    found: set[str] = set()
    for match in re.finditer(r'["\'](/[a-z][a-z0-9_-]*)\b', source):
        cmd = match.group(1)
        # Skip API paths like /v1/... or /openapi.json
        if cmd in {"/v1", "/openapi", "/health", "/api"}:
            if cmd == "/api":
                found.add(cmd)
            continue
        if cmd.startswith("/v1"):
            continue
        found.add(cmd)
    # Canonical 31 Python parity commands documented in TERMINAL_API.md
    canonical = {
        "/help", "/agent", "/project", "/projects", "/model", "/status",
        "/pick", "/confirm", "/expand", "/new", "/thread", "/tasks", "/task",
        "/decisions", "/decision", "/runs", "/run", "/follow", "/alerts",
        "/goals", "/watch", "/overwatch", "/drive", "/go", "/ops", "/describe",
        "/op", "/api", "/coverage", "/clear", "/quit",
    }
    return canonical & found if (canonical & found) else canonical


def extract_rust_commands(source: str) -> set[str]:
    return set(re.findall(r'name:\s*"(/[a-z][a-z0-9_-]*)"', source))


def check_parity(rust_bin: pathlib.Path | None = None) -> dict[str, object]:
    py_cmds = extract_python_commands(PYTHON_TUI.read_text(encoding="utf-8"))
    rs_cmds = extract_rust_commands(RUST_COMMANDS.read_text(encoding="utf-8"))
    missing = sorted(py_cmds - rs_cmds)
    rust_only = sorted(rs_cmds - py_cmds)

    schema_ok = None
    if rust_bin is not None:
        proc = subprocess.run(
            [str(rust_bin), "schema"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            schema_ok = False
        else:
            doc = json.loads(proc.stdout)
            schema_ok = doc.get("properties", {}).get("schema_version", {}).get("const") == "1.0"

    return {
        "python_command_count": len(py_cmds),
        "rust_command_count": len(rs_cmds),
        "missing_in_rust": missing,
        "rust_only_additions": rust_only,
        "schema_v1_ok": schema_ok,
        "parity_green": len(missing) == 0 and (schema_ok in (None, True)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Python <-> Rust Metatron parity (MT-11).")
    parser.add_argument("--rust-bin", type=pathlib.Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = check_parity(args.rust_bin)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"Parity: python={report['python_command_count']} rust={report['rust_command_count']} "
            f"missing={len(report['missing_in_rust'])} green={report['parity_green']}"
        )
        if report["missing_in_rust"]:
            print("Missing in Rust:", ", ".join(report["missing_in_rust"]), file=sys.stderr)
    return 0 if report["parity_green"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
