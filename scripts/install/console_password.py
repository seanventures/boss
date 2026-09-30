#!/usr/bin/env python3
"""Show or rotate the private single-operator Mission Control password."""
from __future__ import annotations

import argparse
import os
import secrets
import shlex
import stat
import sys
import tempfile
from pathlib import Path

KEY = "BOSS_CONSOLE_PASSWORD"


def private_env(path: Path) -> list[str]:
    # MISSING IS NOT A SYMLINK. Both failed with one sentence about symlinks, so running this
    # on a laptop checkout — where BOSS was never installed — sent people looking for a link that
    # does not exist. The usual cause is simply the wrong machine.
    if path.is_symlink():
        raise SystemExit(f"{path} must be a regular .env file, not a symlink")
    if not path.is_file():
        raise SystemExit(
            f"No BOSS install here: {path} does not exist. The console password lives on the "
            "server where BOSS is installed. Run this there, in its install directory "
            "(~/boss by default), or from here: "
            "ssh user@your-server 'cd ~/boss && python3 scripts/install/console_password.py show'")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SystemExit(f"{path} must be private (chmod 600) before reading credentials")
    return path.read_text().splitlines()


def password_from(lines: list[str]) -> str:
    values = [line.split("=", 1)[1] for line in lines if line.startswith(KEY + "=")]
    if len(values) != 1 or len(values[0]) < 32:
        raise SystemExit(f"{KEY} is missing or invalid; rerun install.sh to provision it")
    return values[0]


def reset(path: Path, lines: list[str]) -> None:
    old = password_from(lines)
    new = secrets.token_hex(24)
    assert new != old
    revised = [f"{KEY}={new}" if line.startswith(KEY + "=") else line for line in lines]
    fd, temporary = tempfile.mkstemp(prefix=".env.console-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write("\n".join(revised) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"Reset {KEY} in {path}. The value was not printed.")
    compose = path.resolve().parent / "scripts/install/community-compose.yml"
    print(f"Apply it: docker compose -f {shlex.quote(str(compose))} "
          f"--env-file {shlex.quote(str(path.resolve()))} --profile web "
          "up -d --no-deps --force-recreate web")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("show", "reset"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    lines = private_env(args.env_file)
    if args.action == "show":
        if not sys.stdout.isatty():
            raise SystemExit("Refusing to print a console password outside a terminal")
        print(password_from(lines))
    else:
        reset(args.env_file, lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
