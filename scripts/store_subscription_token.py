#!/usr/bin/env python3
"""Move a coding-CLI subscription token to a BOSS deployment without a human touching it.

WHY THIS EXISTS. Detection finds a CLI on the machine BOSS runs on, which answers the whole
question for a self-hosted install and none of it for a deployed one: the operator's login is
on their laptop, the pipeline runs in a container. The bridge is the travelling token the CLI
mints — and for codex, minting it means reading the CLI's own credential file, because codex
has no `claude setup-token` equivalent.

The documented alternative was "open the JSON file and copy the access_token field", and it
failed the first time a human ran it: the placeholder `<token>` was pasted verbatim, the secret
store answered `{"stored": true}`, and the deployment was left holding a seven-character string
that would have surfaced as an authentication failure in a build, hours later and far from its
cause. A store that cannot tell a credential from a placeholder needs a caller that can.

So: the token goes file -> HTTP, never through a terminal, a clipboard or a shell history. The
only things printed are its length and where it went.

    python3 scripts/store_subscription_token.py codex --store-url http://ops.example:8099
    python3 scripts/store_subscription_token.py codex --dry-run     # prove it before sending
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES  # noqa: E402

DEFAULT_OPS_TOKEN_FILE = "~/.config/boss/ops-token"


def find_field(node: Any, name: str) -> Optional[str]:
    """First non-empty string under `name`, at any depth.

    These credential files nest and the shape is not contractual — pinning a path like
    ["tokens"]["access_token"] would break silently on a CLI update and report "no token
    found" when the token is right there.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == name and isinstance(value, str) and value.strip():
                return value.strip()
            found = find_field(value, name)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_field(item, name)
            if found:
                return found
    return None


def read_token(auth_file: Path, field: str) -> str:
    if not auth_file.is_file():
        raise SystemExit(
            f"{auth_file} does not exist — sign in on this machine first, then re-run.")
    try:
        doc = json.loads(auth_file.read_text())
    except json.JSONDecodeError as exc:
        # The file holds a live credential, so the parser's excerpt of it must not travel.
        raise SystemExit(f"{auth_file} is not valid JSON (line {exc.lineno})") from None
    token = find_field(doc, field)
    if not token:
        raise SystemExit(
            f"no {field!r} field anywhere in {auth_file}. If the CLI changed its format, pass "
            f"--field with the new name.")
    return token


def store(store_url: str, ops_token: str, key: str, value: str, timeout: int = 20) -> dict:
    request = urllib.request.Request(
        f"{store_url.rstrip('/')}/secret", method="POST",
        data=json.dumps({"key": key, "value": value}).encode(),
        headers={"X-Ops-Token": ops_token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        # The body can echo the request, and the request carried the token.
        raise SystemExit(f"secret store refused the write: HTTP {exc.code}") from None
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"secret store unreachable: {type(exc).__name__}") from None


def resolve_ops_token(explicit: str, token_file: str) -> str:
    if explicit:
        return explicit.strip()
    path = Path(os.path.expanduser(token_file))
    if not path.is_file():
        raise SystemExit(
            f"no ops token: pass --ops-token or put one in {token_file}")
    return path.read_text().strip().splitlines()[0]


def main(argv: Optional[list[str]] = None) -> int:
    tokenable = {name: spec for name, spec in BUILTIN_CLI_HARNESSES.items()
                 if spec.token_source_file}
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("provider", choices=sorted(tokenable),
                        help="which subscription CLI to take the token from")
    parser.add_argument("--store-url", default=os.environ.get("OPS_BASE_URL", ""),
                        help="base URL of the secret store (or set OPS_BASE_URL)")
    parser.add_argument("--ops-token", default="", help="store token; prefer the file")
    parser.add_argument("--ops-token-file", default=DEFAULT_OPS_TOKEN_FILE)
    parser.add_argument("--auth-file", default="", help="override the CLI's credential file")
    parser.add_argument("--field", default="", help="override the field to read")
    parser.add_argument("--dry-run", action="store_true",
                        help="find the token and report its shape; send nothing")
    args = parser.parse_args(argv)

    spec = tokenable[args.provider]
    auth_file = Path(os.path.expanduser(args.auth_file or spec.token_source_file))
    token = read_token(auth_file, args.field or spec.token_source_field)

    # A placeholder reaching the store is the failure this script exists to prevent, so it is
    # checked here too rather than trusted to never happen again.
    if token.startswith("<") and token.endswith(">"):
        raise SystemExit(f"{auth_file} contains the placeholder {token!r}, not a credential")

    if args.dry_run:
        print(f"would store {spec.token_env} ({len(token)} chars) from {auth_file}")
        print(f"             -> {args.store_url or '<--store-url not set>'}/secret")
        return 0
    if not args.store_url:
        raise SystemExit("--store-url is required (or set OPS_BASE_URL)")

    ops_token = resolve_ops_token(args.ops_token, args.ops_token_file)
    result = store(args.store_url, ops_token, spec.token_env, token)
    if not result.get("ok", True):
        raise SystemExit(f"secret store reported failure: {result}")
    print(f"stored {spec.token_env} ({len(token)} chars) from {auth_file}")
    print("redeploy for it to reach the running process — the store feeds the app's "
          "environment at deploy time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
