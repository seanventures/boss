"""What an app's connectors still need a person for: the Apps row's "Set up sign-in · N providers"
chip (factory_view.apps) and App › Connectors' summary line say the same thing from ONE rule. The
console's copy of the rule (apps/web/lib/app-connectors.ts `providerNeedsYou` / `keyNeedsYou`) is
tested against the same cases.

  - A connector counts while any of its attempts is in the app or on its way: not every attempt
    failed or abandoned (the setup guide's own filter).
  - A SIGN-IN PROVIDER (base_url.sign_in_providers) is set up once its client id has a stored value
    — a person's key, or the one BOSS issued (Sign in with BOSS) — for preview or deploy. A stub is
    the build's fake provider: it does not count.
  - A KEY (a required secret whose test default is `none`, not a provider's) is set once it has a
    stored value for preview or deploy.

Names only: no value, no shape, nothing a secret could be read from.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from .base_url import sign_in_providers


def _manifest(v: Any) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def needs(c, project_id: str) -> dict[str, dict[str, list[str]]]:
    """{app_key: {"providers": [provider…], "keys": [name…]}} for every app with something to set
    up, in two queries for the whole project (the Apps list polls it)."""
    rows = c.execute(text(
        "SELECT DISTINCT a.app_key, t.template_key, t.manifest FROM app_template_applications a "
        "JOIN app_templates t ON t.id = a.template_id WHERE a.project_id = :p "
        "AND a.status NOT IN ('failed', 'abandoned')"), {"p": project_id}).mappings().all()
    if not rows:
        return {}
    stored: dict[str, set[str]] = {}
    for key, name in c.execute(text(
            "SELECT app_key, name FROM app_secret_bindings WHERE project_id = :p "
            "AND env IN ('preview', 'deploy') AND vault_item_id IS NOT NULL AND mode <> 'stub'"),
            {"p": project_id}).all():
        stored.setdefault(str(key), set()).add(str(name))
    out: dict[str, dict[str, list[str]]] = {}
    for r in rows:
        app = str(r["app_key"])
        m = _manifest(r["manifest"])
        have = stored.get(app, set())
        providers = sign_in_providers(m)
        owned = {n for p in providers for n in (p["id"], p["secret"])}
        want = out.setdefault(app, {"providers": [], "keys": []})
        for p in providers:
            if p["id"] not in have and p["provider"] not in want["providers"]:
                want["providers"].append(p["provider"])
        for s in m.get("secrets") or []:
            if not isinstance(s, dict) or not s.get("name") or s["name"] in owned:
                continue
            if (s.get("test_default") or "none") == "none" and s.get("required", True) is not False \
                    and s["name"] not in have and s["name"] not in want["keys"]:
                want["keys"].append(str(s["name"]))
    return {k: v for k, v in out.items() if v["providers"] or v["keys"]}
