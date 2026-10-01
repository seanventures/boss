#!/usr/bin/env python3
"""Facts about a local model before it is downloaded. Read only.

`size MODEL` prints the download size in bytes, read from the model's manifest on the Ollama
registry (the layer sizes it lists), or `unknown` when the registry does not answer. Nothing
is downloaded except that small manifest, and no model runs.

`price-zero MODEL ENV_FILE` records MODEL at zero cost in BOSS_MODEL_PRICES_JSON in the
install's .env, keeping every other entry, and exits 0 when the file changed, 3 when it
already said so (1 is an error). The rest of .env is copied unchanged and the file stays mode 0600.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

REGISTRY = "https://registry.ollama.ai"
MODEL = re.compile(r"^(?:([a-z0-9][a-z0-9._-]{0,63})/)?([a-z0-9][a-z0-9._-]{0,127})(?::([A-Za-z0-9._-]{1,128}))?$")
PRICES = "BOSS_MODEL_PRICES_JSON"


def registry() -> str:
    """The registry, https only. A loopback http address is accepted for a local mirror."""
    url = os.environ.get("BOSS_OLLAMA_REGISTRY", REGISTRY).rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in
                                    {"127.0.0.1", "localhost", "::1"}):
        return url
    raise SystemExit("BOSS_OLLAMA_REGISTRY must be an https address")


def size(model: str) -> int | None:
    match = MODEL.match(model)
    if not match:
        raise SystemExit(f"not an Ollama model name: {model!r}")
    namespace, name, tag = match.group(1) or "library", match.group(2), match.group(3) or "latest"
    request = urllib.request.Request(
        f"{registry()}/v2/{namespace}/{name}/manifests/{tag}",
        headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as reply:
            manifest = json.loads(reply.read(2_000_000).decode() or "{}")
    except (OSError, ValueError, urllib.error.URLError):
        return None
    parts = list(manifest.get("layers") or []) + [manifest.get("config") or {}]
    total = sum(int(part.get("size") or 0) for part in parts if isinstance(part, dict))
    return total or None


def price_zero(model: str, env_file: str) -> int:
    with open(env_file, encoding="utf-8") as stream:
        lines = stream.read().splitlines()
    index = next((i for i, line in enumerate(lines) if line.startswith(PRICES + "=")), None)
    prices: dict = {}
    if index is not None:
        raw = lines[index].split("=", 1)[1].strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
            raw = raw[1:-1]
        try:
            prices = json.loads(raw) if raw else {}
        except ValueError:
            raise SystemExit(f"{PRICES} in .env is not JSON; fix it by hand. Nothing was changed.")
        if not isinstance(prices, dict):
            raise SystemExit(f"{PRICES} in .env is not a JSON object. Nothing was changed.")
    if prices.get(model) == [0, 0]:
        return 3
    prices[model] = [0, 0]
    # Single quotes: Compose reads the value literally, so the JSON's double quotes survive.
    line = f"{PRICES}='{json.dumps(prices, separators=(',', ':'))}'"
    if index is None:
        lines.append(line)
    else:
        lines[index] = line
    temporary = f"{env_file}.writing.{os.getpid()}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, env_file)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "size":
        found = size(argv[1])
        print(found if found is not None else "unknown")
        return 0
    if len(argv) == 3 and argv[0] == "price-zero":
        if not MODEL.match(argv[1]):
            raise SystemExit(f"not an Ollama model name: {argv[1]!r}")
        return price_zero(argv[1], argv[2])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
