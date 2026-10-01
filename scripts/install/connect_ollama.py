#!/usr/bin/env python3
"""Register the Ollama on this machine as this install's OpenAI-compatible model.

`boss connect ollama` (and the installer, after a yes or --connect-found) runs this once the
BOSS API container has shown it can reach Ollama at host.docker.internal. It goes through the
same route as Mission Control's "Anything OpenAI-compatible" form, POST /v1/setup/model:

- provider `custom`, base URL http://host.docker.internal:<port>/v1, one model Ollama has
- the placeholder key `ollama`, because the form requires a key and Ollama ignores it

Nothing is sent to Ollama here except the read-only model list on loopback, and no model is
called. Community stores the connection unverified; the first governed job verifies it. An
existing OpenAI-compatible connection to another address is left alone.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_models import MODEL_NAME, ollama_model_names  # noqa: E402

PROJECT = "boss-dogfood"
SOURCE_ID = "setup-custom"
PLACEHOLDER_KEY = "ollama"


class ConnectError(RuntimeError):
    pass


def _request(url: str, api_key: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        url, method="POST" if payload is not None else "GET",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"x-boss-api-key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        # The body is not echoed: keep this output to what BOSS itself decided.
        raise ConnectError(f"BOSS refused the request: HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise ConnectError("BOSS did not answer; is it running? Try: boss status") from exc
    return body if isinstance(body, dict) else {}


def existing_connection(api_url: str, api_key: str) -> dict | None:
    body = _request(f"{api_url}/v1/entitlements/sources?project_id={PROJECT}", api_key)
    items = body.get("items") if isinstance(body.get("items"), list) else []
    for item in items:
        if isinstance(item, dict) and item.get("source_id") == SOURCE_ID \
                and item.get("credential_secret_ref_id"):
            metadata = item.get("connection_metadata")
            models = item.get("supported_models")
            return {
                "base_url": str((metadata or {}).get("base_url") or "").rstrip("/")
                if isinstance(metadata, dict) else "",
                "models": [m for m in models if isinstance(m, str)] if isinstance(models, list) else [],
            }
    return None


def choose_model(names: list[str], wanted: str) -> str:
    usable = [name for name in names if name]
    if wanted:
        if not MODEL_NAME.fullmatch(wanted) or wanted not in usable:
            raise ConnectError(f"Ollama on this machine has no model named {wanted!r}. "
                               "List them with: ollama list")
        return wanted
    if not usable:
        raise ConnectError("Ollama has no models yet. Pull one first, for example: "
                           "ollama pull llama3.2")
    return usable[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--container-url", required=True,
                        help="Ollama's OpenAI-compatible address as the BOSS containers see it")
    parser.add_argument("--host-url", default=os.environ.get("BOSS_OLLAMA_URL",
                                                             "http://127.0.0.1:11434"))
    parser.add_argument("--model", default=os.environ.get("BOSS_OLLAMA_MODEL", ""))
    args = parser.parse_args()

    container = urllib.parse.urlparse(args.container_url)
    if container.scheme != "http" or container.hostname != "host.docker.internal":
        raise ConnectError("the container address must be http://host.docker.internal:PORT/v1")
    api_key = os.environ.get("BOSS_API_KEY", "")
    if not api_key:
        raise ConnectError("BOSS_API_KEY is missing; rerun the installer")
    api_url = args.api_url.rstrip("/")
    base_url = args.container_url.rstrip("/")

    names = ollama_model_names(args.host_url)
    if names is None:
        raise ConnectError(f"no Ollama answers at {args.host_url} on this machine")
    model = choose_model(names, args.model.strip())

    current = existing_connection(api_url, api_key)
    if current is not None:
        if current["base_url"] == base_url and model in current["models"]:
            print(f"  Ollama is already connected ({model} at {base_url}); nothing changed")
            return 0
        if current["base_url"] != base_url:
            raise ConnectError(
                "this install already has an \"Anything OpenAI-compatible\" connection to "
                "another address. Nothing was changed. To use Ollama instead, replace that "
                "connection in Mission Control (Settings, Repo and model).")

    result = _request(f"{api_url}/v1/setup/model", api_key, {
        "project_id": PROJECT, "actor": "ceo", "provider": "custom",
        "api_key": PLACEHOLDER_KEY, "base_url": base_url, "model": model,
    })
    if result.get("ok") is not True or result.get("source_id") != SOURCE_ID:
        raise ConnectError("BOSS did not record the Ollama connection")
    print(f"  Ollama connected: {model} at {base_url} "
          "(unverified until a governed job uses it; no model was called)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConnectError as error:
        print(f"  Ollama not connected: {error}", file=sys.stderr)
        raise SystemExit(1) from None
