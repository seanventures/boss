#!/usr/bin/env python3
"""Facts about a local model before it is downloaded. Read only.

`size MODEL` prints the download size in bytes, read from the model's manifest on the Ollama
registry (the layer sizes it lists), or `unknown` when the registry does not answer. Nothing
is downloaded except that small manifest, and no model runs.

`price-zero MODEL ENDPOINT ENV_FILE` records MODEL at zero cost in BOSS_MODEL_PRICES_JSON,
keyed "<ENDPOINT>#<MODEL>" so the same model name on a hosted provider is never priced at
zero. Other entries are kept. Exits 0 when .env changed, 3 when it already said so.

`chat-local MODEL ENDPOINT ENV_FILE` points OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
(chat and the other surfaces that read them) at the local model, only when no hosted OpenAI
key is in .env. `chat-hosted ENV_FILE` undoes that once a hosted OpenAI key is saved.

Every change rewrites .env atomically with mode 0600; values are never printed.
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


LOCAL_HOSTS = {"host.docker.internal", "localhost", "127.0.0.1", "::1"}
LOCAL_KEY = "ollama"


def is_local(url: str) -> bool:
    try:
        return urllib.parse.urlparse(url.strip()).hostname in LOCAL_HOSTS
    except ValueError:
        return False


def read_env(env_file: str) -> list[str]:
    with open(env_file, encoding="utf-8") as stream:
        return stream.read().splitlines()


def env_get(lines: list[str], name: str) -> str | None:
    for line in lines:
        if line.startswith(name + "="):
            return line.split("=", 1)[1]
    return None


def env_set(lines: list[str], name: str, value: str) -> None:
    kept = [line for line in lines if not line.startswith(name + "=")]
    lines[:] = kept + [f"{name}={value}"]


def write_env(env_file: str, lines: list[str]) -> None:
    """Replace .env atomically, mode 0600."""
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


def price_zero(model: str, endpoint: str, env_file: str) -> int:
    """Price MODEL at zero at ENDPOINT only: the key is "<endpoint>#<model>", which the price
    table matches for calls to that endpoint and never for the same name elsewhere."""
    lines = read_env(env_file)
    raw = (env_get(lines, PRICES) or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        raw = raw[1:-1]
    try:
        prices = json.loads(raw) if raw else {}
    except ValueError:
        raise SystemExit(f"{PRICES} in .env is not JSON; fix it by hand. Nothing was changed.")
    if not isinstance(prices, dict):
        raise SystemExit(f"{PRICES} in .env is not a JSON object. Nothing was changed.")
    key = f"{endpoint.strip().rstrip('/')}#{model}"
    if prices.get(key) == [0, 0]:
        return 3
    prices[key] = [0, 0]
    # Single quotes: Compose reads the value literally, so the JSON's double quotes survive.
    env_set(lines, PRICES, f"'{json.dumps(prices, separators=(',', ':'))}'")
    write_env(env_file, lines)
    return 0


def chat_local(model: str, endpoint: str, env_file: str) -> int:
    """Point the OPENAI_* surfaces (chat, meetings, the native fallback) at the local model.

    Only when no hosted OpenAI key is there: OPENAI_API_KEY empty, or already the local
    placeholder with a local base URL. 0 changed, 3 already so, 4 a hosted key is present.
    """
    lines = read_env(env_file)
    key = (env_get(lines, "OPENAI_API_KEY") or "").strip()
    base = (env_get(lines, "OPENAI_BASE_URL") or "").strip()
    if key and not (key == LOCAL_KEY and is_local(base)):
        return 4
    wanted = {"OPENAI_API_KEY": LOCAL_KEY, "OPENAI_BASE_URL": endpoint.rstrip("/"),
              "OPENAI_MODEL": model}
    if all((env_get(lines, name) or "") == value for name, value in wanted.items()):
        return 3
    for name, value in wanted.items():
        env_set(lines, name, value)
    write_env(env_file, lines)
    return 0


def chat_hosted(env_file: str) -> int:
    """A hosted OPENAI_API_KEY arrived: drop the local base URL and model so chat uses it.
    0 changed, 3 nothing local to drop."""
    lines = read_env(env_file)
    key = (env_get(lines, "OPENAI_API_KEY") or "").strip()
    base = (env_get(lines, "OPENAI_BASE_URL") or "").strip()
    if not key or key == LOCAL_KEY or not is_local(base):
        return 3
    env_set(lines, "OPENAI_BASE_URL", "")
    env_set(lines, "OPENAI_MODEL", "gpt-4o-mini")
    write_env(env_file, lines)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "size":
        found = size(argv[1])
        print(found if found is not None else "unknown")
        return 0
    if len(argv) == 4 and argv[0] in ("price-zero", "chat-local"):
        if not MODEL.match(argv[1]):
            raise SystemExit(f"not an Ollama model name: {argv[1]!r}")
        if not is_local(argv[2]):
            raise SystemExit("the endpoint must be the local Ollama address")
        return (price_zero if argv[0] == "price-zero" else chat_local)(argv[1], argv[2], argv[3])
    if len(argv) == 2 and argv[0] == "chat-hosted":
        return chat_hosted(argv[1])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
