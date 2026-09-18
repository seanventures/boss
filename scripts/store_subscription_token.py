#!/usr/bin/env python3
"""Move a coding-CLI subscription to a BOSS deployment without a human touching a credential.

WHY THIS EXISTS. Detection finds a CLI on the machine BOSS runs on, which answers the whole
question for a self-hosted install and none of it for a deployed one: the operator's login is
on their laptop, the pipeline runs in a container.

WHAT TRAVELS depends on the CLI, and the spec decides — this script is never told:

  * claude-code mints a headless token (`claude setup-token`), so the TOKEN travels.
  * codex has no such thing. `codex login --with-access-token` rejects every field its auth
    file holds, because it wants an agent identity JWT and a consumer ChatGPT plan never mints
    one; setting CODEX_ACCESS_TOKEN makes `codex login status` error outright. Restoring the
    CLI's own auth FILE makes it report "Logged in using ChatGPT", so the file travels.

Either way the credential goes file -> HTTP, never through a terminal, a clipboard or a shell
history, and the only things printed are its size and where it went. The instruction this
replaced was "open the JSON file and copy the access_token field", which failed on its first
human run: the doc's placeholder went in verbatim, the store answered {"stored": true}, and the
deployment held a seven-character string. A store that cannot tell a credential from a
placeholder needs a caller that can.

    python3 scripts/store_subscription_token.py codex --dry-run     # prove it, send nothing
    python3 scripts/store_subscription_token.py codex --store-url http://127.0.0.1:8099
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


def read_whole_file(auth_file: Path) -> str:
    """The CLI's entire credential file, which is what actually authorises it.

    Codex rejects every individual field with `--with-access-token` — it wants an agent
    identity JWT, which a consumer ChatGPT plan never mints. Restoring the file itself makes
    the CLI report "Logged in using ChatGPT". So the file travels, not a field from it.
    """
    if not auth_file.is_file():
        raise SystemExit(
            f"{auth_file} does not exist — sign in on this machine first, then re-run.")
    content = auth_file.read_text()
    if auth_file.suffix != ".json":
        return content
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        # The file holds a live credential, so the parser's excerpt must not travel.
        raise SystemExit(f"{auth_file} is not valid JSON (line {exc.lineno})") from None
    # ONE LINE. A secret store that renders into a .env cannot hold a newline and says so:
    # `{"error": "value must be a single-line string"}`, HTTP 400. Pretty-printed JSON is the
    # normal shape for these files, so sending the file verbatim fails for most operators.
    # Re-serialising compactly changes the bytes and not the meaning — the CLI parses JSON.
    return json.dumps(parsed, separators=(",", ":"))


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


def read_piped_token(stream) -> str:
    """The credential from standard input, for a CLI that PRINTS one rather than storing it.

    `claude setup-token` writes its token to stdout, so there is no file to read — which is
    also why it is the one CLI whose credential never has to be found on disk at all. It may
    print a line of explanation around the token, and a token has no whitespace in it, so the
    longest whitespace-free run is the token. Stated rather than guessed at: if that run is
    short enough to be a word, this refuses instead of storing prose.
    """
    text = stream.read()
    if not text.strip():
        raise SystemExit(
            "nothing on standard input. Pipe the token in:\n"
            "    claude setup-token | python3 scripts/store_subscription_token.py "
            "claude_code --stdin")
    candidate = max(text.split(), key=len)
    if len(candidate) < 20:
        # Long enough to be a credential, short enough to be a sentence: say which, without
        # echoing it. A store that cannot tell a credential from a placeholder needs a caller
        # that can, and that is the whole reason this script exists.
        raise SystemExit(
            f"standard input does not look like a credential (longest token: "
            f"{len(candidate)} characters). Did the command print an error instead?")
    return candidate


def send_to_boss(api: str, api_key: str, project_id: str, provider: str, credential: str,
                 # "setup" is not a member of any project BOSS seeds, so the endpoint refused
                 # every call with "Actor 'setup' is not an active member of org 'org_boss'" —
                 # a name invented here rather than taken from the roles that actually exist.
                 # `ceo` is seeded, holds entitlement_manage, and is what the docs already use.
                 actor: str = "ceo", timeout: int = 30,
                 accept_database_fallback: bool = False) -> dict:
    """Give the credential to BOSS and let it decide where it lives.

    The default route, because it is the only one that asks nothing of the customer's
    infrastructure: BOSS writes to the best store the installation has — a managed store, a
    vault, its local encrypted vault, or Postgres — and reads it back itself, so there is no
    env file to edit, no secret service to run, and no redeploy.
    """
    request = urllib.request.Request(
        f"{api.rstrip('/')}/v1/setup/coding-agents/credential", method="POST",
        data=json.dumps({"project_id": project_id, "actor": actor,
                         "provider": provider, "credential": credential,
                         "accept_database_fallback": accept_database_fallback}).encode(),
        headers={"x-boss-api-key": api_key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = {}
        try:
            body = json.loads(exc.read().decode() or "{}")
        except Exception:  # noqa: BLE001 — the status is the part that always travels
            body = {}
        detail = body.get("detail")
        # A DECISION, NOT A FAILURE. BOSS refuses to put a live credential in Postgres without
        # being told to, and the operator is the one who decides. Retrying with the flag set
        # automatically would answer the question on their behalf, which is the one thing this
        # exchange exists to prevent — so it is printed and the run stops.
        if exc.code == 409 and isinstance(detail, dict):
            raise SystemExit(
                f"{detail.get('message')}\n\n"
                f"To store it in the database anyway, re-run with "
                f"--accept-database-fallback.") from None
        if isinstance(detail, dict):
            detail = detail.get("message", "")
        raise SystemExit(
            f"BOSS refused the credential: HTTP {exc.code} {str(detail or '')[:200]}") from None
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"could not reach BOSS at {api}: {type(exc).__name__}") from None


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


def write_env_file(path: Path, key: str, value: str) -> str:
    """Set `key` in an env file, replacing any line that already sets it.

    Appending blindly leaves two assignments for the same name, and which one wins is decided
    by whatever reads the file — so a stale credential can quietly outrank the new one.

    Created 0600 when new. An env file holding a live session should not be readable by every
    process on the box, and a mode fixed after the write leaves a window where it is.
    """
    line = f"{key}={value}"
    existing = path.read_text().splitlines() if path.is_file() else []
    replaced = False
    out = []
    for entry in existing:
        if entry.startswith(f"{key}=") or entry.startswith(f"export {key}="):
            if not replaced:
                out.append(line)
                replaced = True
            continue          # drop any further duplicates
        out.append(entry)
    if not replaced:
        out.append(line)
    if not path.is_file():
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
    path.write_text("\n".join(out) + "\n")
    return "updated" if replaced else "added"


def resolve_ops_token(explicit: str, token_file: str) -> str:
    if explicit:
        return explicit.strip()
    path = Path(os.path.expanduser(token_file))
    if not path.is_file():
        raise SystemExit(
            f"no ops token: pass --ops-token or put one in {token_file}")
    return path.read_text().strip().splitlines()[0]


def main(argv: Optional[list[str]] = None) -> int:
    # Every CLI whose credential can TRAVEL. Requiring a `token_source_file` excluded the one
    # CLI that needs no file — claude-code prints its token — so the script could not do the
    # thing its own docstring described.
    tokenable = {name: spec for name, spec in BUILTIN_CLI_HARNESSES.items()
                 if spec.token_env or spec.auth_file_secret}
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("provider", choices=sorted(tokenable),
                        help="which subscription CLI to take the token from")
    parser.add_argument("--boss-api", default=os.environ.get("BOSS_API", ""),
                        help="your BOSS API address (or set BOSS_API). The default route.")
    parser.add_argument("--boss-api-key", default=os.environ.get("BOSS_API_KEY", ""),
                        help="your BOSS API key (or set BOSS_API_KEY)")
    parser.add_argument("--project-id", default="boss-dogfood",
                        help="the BOSS project to attach the subscription to")
    parser.add_argument("--actor", default="ceo",
                        help="who is doing this; must be a member of the project")
    # NOT defaulted from OPS_BASE_URL. It was, and a variable exported hours earlier for a
    # different step silently took precedence over the documented default — the credential
    # went to the operator's own store while the page said BOSS would hold it, and the run
    # succeeded, so nothing pointed at the discrepancy. An advanced route has to be asked for.
    parser.add_argument("--store-url", default="",
                        help="advanced: write to your own secret store instead of BOSS")
    parser.add_argument("--ops-token", default="", help="store token; prefer the file")
    parser.add_argument("--ops-token-file", default=DEFAULT_OPS_TOKEN_FILE)
    parser.add_argument("--auth-file", default="", help="override the CLI's credential file")
    parser.add_argument("--stdin", action="store_true",
                        help="read the credential from standard input, for a CLI that prints "
                             "one: claude setup-token | ... claude_code --stdin")
    parser.add_argument("--field", default="", help="override the field to read")
    parser.add_argument("--write-env", metavar="FILE", default="",
                        help="write KEY=value into this env file instead of a secret store")
    parser.add_argument("--accept-database-fallback", action="store_true",
                        help="store in BOSS's database when no secret store is configured")
    parser.add_argument("--dry-run", action="store_true",
                        help="find the credential and report its size; send nothing")
    args = parser.parse_args(argv)

    spec = tokenable[args.provider]
    source_file = args.auth_file or spec.token_source_file
    auth_file = Path(os.path.expanduser(source_file)) if source_file else None
    # Which of the three routes this CLI actually supports. Codex takes the whole file; a
    # CLI that prints its credential is piped in; anything else has a field in a file. The
    # spec decides, so this script never has to be told.
    if args.stdin:
        key = spec.auth_file_secret or spec.token_env
        token = read_piped_token(sys.stdin)
        what = "the token"
        origin = "standard input"
    elif auth_file is None:
        raise SystemExit(
            f"{args.provider} keeps no credential file to read — it prints one. Pipe it in:\n"
            f"    {spec.signin_hint} | python3 scripts/store_subscription_token.py "
            f"{args.provider} --stdin")
    elif spec.auth_file_secret:
        key = spec.auth_file_secret
        token = read_whole_file(auth_file)
        what = f"the whole {auth_file.name}"
        origin = str(auth_file)
    else:
        key = spec.token_env
        token = read_token(auth_file, args.field or spec.token_source_field)
        what = f"the {args.field or spec.token_source_field} field"
        origin = str(auth_file)

    # A placeholder reaching the store is the failure this script exists to prevent, so it is
    # checked here too rather than trusted to never happen again.
    if token.startswith("<") and token.endswith(">"):
        raise SystemExit(f"{origin} contains the placeholder {token!r}, not a credential")

    if args.write_env:
        destination = f"{args.write_env} (env file)"
    elif args.store_url:
        destination = f"{args.store_url}/secret"
    elif args.boss_api:
        destination = f"{args.boss_api} (BOSS chooses the store)"
    else:
        destination = "no destination set"
    if args.dry_run:
        print(f"would store {key} ({len(token)} chars) — {what} from {origin}")
        print(f"             -> {destination}")
        return 0

    if args.write_env:
        target = Path(os.path.expanduser(args.write_env))
        action = write_env_file(target, key, token)
        print(f"{action} {key} ({len(token)} chars) in {target}")
        print("restart BOSS for it to take effect.")
        return 0
    if args.store_url:
        ops_token = resolve_ops_token(args.ops_token, args.ops_token_file)
        result = store(args.store_url, ops_token, key, token)
        if not result.get("ok", True):
            raise SystemExit(f"secret store reported failure: {result}")
        print(f"stored {key} ({len(token)} chars) — {what} from {origin}")
        print(f"destination: {args.store_url} — YOUR store, not BOSS's.")
        print("BOSS cannot read it until you point it at that store "
              "(BOSS_PLATFORM_SECRET_STORE_URL / BOSS_VAULT_ADDR / BOSS_KMS_MASTER_KEY).")
        return 0

    if not args.boss_api:
        # "or set BOSS_API" alone was read once by somebody who HAD set it — without `export`,
        # so their shell echoed it while this process saw nothing. A child cannot tell unset
        # from unexported, so the message has to raise the possibility itself.
        raise SystemExit(
            "no destination. Pass --boss-api, or export the variables:\n"
            "    export BOSS_API=https://your-boss-api\n"
            "    export BOSS_API_KEY=your-boss-api-key\n"
            "`export` matters — a variable set without it shows up in `echo $BOSS_API` but is "
            "not passed to this script.")
    if not args.boss_api_key:
        raise SystemExit("no --boss-api-key (or BOSS_API_KEY) to authenticate to BOSS with.")

    result = send_to_boss(args.boss_api, args.boss_api_key, args.project_id,
                          args.provider, token, actor=args.actor,
                          accept_database_fallback=args.accept_database_fallback)
    print(f"stored {key} ({len(token)} chars) — {what} from {origin}")
    backend = result.get("backend", "configured")
    print(f"destination: {args.boss_api} — BOSS put it in its {backend} store. "
          f"Nothing to restart.")
    if result.get("fallback_accepted"):
        print("that store is BOSS's own database. Anyone with the database has this "
              "credential; scripts/install/bootstrap_infisical.sh sets up a real one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
