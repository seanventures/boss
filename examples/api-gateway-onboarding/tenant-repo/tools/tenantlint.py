#!/usr/bin/env python3
"""tenantlint: checks one gateway tenant file against the repository's tenant rules.

    python3 tools/tenantlint.py config/tenants/<tenant>.yaml --json

Prints {"path", "violations": [{rule_id, category, severity, title, why, fix, line, fixable}]}
(the shape BOSS reads with its `gwlint-json` adapter) and exits 1 when there is any violation.
The rules and their reasons are in rules/TENANT_RULES.md.

Standard library only, so it runs in any build sandbox. It reads the plain block YAML that tenant
files use (maps, lists of maps, quoted strings, numbers, booleans, ["flow", "lists"]); anchors,
multi-line strings and other YAML features are refused with a clear message.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any

SECRET_KEYS = ("api_key", "password", "secret", "token", "client_secret")
SENSITIVE_PATH = re.compile(r"/(admin|internal|debug|actuator)(/|$)")
VERSIONED_PATH = re.compile(r"^/v\d+/")

RULES = {
    # rule_id: (category, severity, fixable, title, why, fix)
    "no-inlined-secrets": (
        "security", "error", "never", "No API keys, tokens or passwords inlined in plaintext",
        "A secret committed to a config repo stays in git history even after it is deleted.",
        "Remove the value and reference a secret manager entry with api_key_ref."),
    "cors-no-wildcard-with-credentials": (
        "security", "error", "never", "CORS must not combine a wildcard origin with credentials",
        "A wildcard origin with allow_credentials lets any website replay a user's credentials.",
        "List explicit allowed origins, or set allow_credentials: false."),
    "upstream-https-required": (
        "security", "error", "mechanical", "Upstreams must be reached over https",
        "Plain http between the gateway and the upstream exposes tokens and payloads.",
        "Change the upstream url scheme to https (same host, port and path)."),
    "no-sensitive-paths-exposed": (
        "security", "error", "never", "Admin and internal paths are never published to tenants",
        "An admin or internal surface behind the public gateway is reachable by every tenant.",
        "Remove the route."),
    "rate-limit-required": (
        "guideline", "error", "mechanical", "Every route declares a rate limit",
        "Without a rate limit one tenant can exhaust shared gateway capacity.",
        "Add rate_limit.rps and rate_limit.burst (e.g. 10 and 10)."),
    "request-size-capped": (
        "guideline", "error", "mechanical", "Every route declares a request body size cap",
        "Without max_body_bytes a single request can use unbounded gateway memory.",
        "Add max_body_bytes, e.g. 1048576."),
    "pii-fields-declared": (
        "guideline", "warn", "never", "Tenants handling PII declare which fields to redact",
        "Without pii_fields the access logs only get the gateway's fixed redaction.",
        "Add pii_fields listing the request fields that carry personal data."),
    "owner-contact-required": (
        "guideline", "warn", "never", "Every tenant names an owner and a contact",
        "Operations needs someone to call when the tenant's routes misbehave.",
        "Add owner and contact."),
    "path-versioned": (
        "guideline", "error", "never", "Every route path starts with a version (/v1/...)",
        "An unversioned path cannot change without breaking every caller.",
        "Prefix the path with its API version, e.g. /v1/."),
}


class Unsupported(ValueError):
    pass


# ── a small reader for the block YAML tenant files use, keeping line numbers ──────────────────

def _scalar(text: str) -> Any:
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            return json.loads(text)
        except ValueError:
            raise Unsupported(f"flow list {text!r}: use [\"a\", \"b\"]") from None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text in ("", "~", "null"):
        return None
    if text[0] in "&*|>{!":
        raise Unsupported(f"YAML feature not supported here: {text[:20]!r}")
    return text


def parse(text: str) -> tuple[Any, dict[tuple, int]]:
    """(value, lines): lines maps a path such as ("routes", 0, "auth", "api_key") to its line."""
    rows = []
    for no, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise Unsupported(f"line {no}: tabs in indentation")
        rows.append([no, len(raw) - len(raw.lstrip()), raw.strip()])
    lines: dict[tuple, int] = {}

    def block(i: int, indent: int, path: tuple) -> tuple[Any, int]:
        if rows[i][2].startswith("- "):
            out_list: list = []
            while i < len(rows) and rows[i][1] == indent and rows[i][2].startswith("- "):
                no, _, content = rows[i]
                here = path + (len(out_list),)
                lines[here] = no
                rows[i] = [no, indent + 2, content[2:].strip()]
                if ":" in rows[i][2] and not rows[i][2].startswith(("\"", "[")):
                    value, i = block(i, indent + 2, here)
                else:
                    value, i = _scalar(rows[i][2]), i + 1
                out_list.append(value)
            return out_list, i
        out: dict = {}
        while i < len(rows) and rows[i][1] == indent and not rows[i][2].startswith("- "):
            no, _, content = rows[i]
            key, sep, rest = content.partition(":")
            if not sep:
                raise Unsupported(f"line {no}: expected `key: value`")
            key = key.strip().strip("\"'")
            lines[path + (key,)] = no
            if rest.strip():
                out[key], i = _scalar(rest), i + 1
            elif i + 1 < len(rows) and rows[i + 1][1] > indent:
                out[key], i = block(i + 1, rows[i + 1][1], path + (key,))
            else:
                out[key], i = None, i + 1
        if i < len(rows) and rows[i][1] > indent:
            raise Unsupported(f"line {rows[i][0]}: unexpected indentation")
        return out, i

    if not rows:
        return {}, lines
    value, end = block(0, rows[0][1], ())
    if end != len(rows):
        raise Unsupported(f"line {rows[end][0]}: unexpected indentation")
    return value, lines


# ── the rules ──────────────────────────────────────────────────────────────────────────────────

def check(doc: Any, lines: dict[tuple, int]) -> list[dict]:
    found: list[dict] = []

    def hit(rule: str, path: tuple, detail: str = "") -> None:
        category, severity, fixable, title, why, fix = RULES[rule]
        line = None
        for n in range(len(path), -1, -1):
            if path[:n] in lines:
                line = lines[path[:n]]
                break
        found.append({"rule_id": rule, "category": category, "severity": severity,
                      "title": title + (f" ({detail})" if detail else ""), "why": why,
                      "fix": fix, "line": line or 1, "fixable": fixable})

    if not isinstance(doc, dict):
        return [{"rule_id": "tenant-file", "category": "guideline", "severity": "error",
                 "title": "A tenant file is a map", "why": "", "fix": "", "line": 1,
                 "fixable": "never"}]
    if not doc.get("owner") or not doc.get("contact"):
        hit("owner-contact-required", ("owner",))
    if str(doc.get("data_classification") or "").lower() == "pii" and not doc.get("pii_fields"):
        hit("pii-fields-declared", ("data_classification",))

    def secrets(value: Any, path: tuple) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if k in SECRET_KEYS and isinstance(v, (str, int)) and str(v):
                    hit("no-inlined-secrets", path + (k,), k)
                secrets(v, path + (k,))
        elif isinstance(value, list):
            for n, v in enumerate(value):
                secrets(v, path + (n,))

    secrets(doc, ())
    for n, route in enumerate(doc.get("routes") or []):
        here = ("routes", n)
        if not isinstance(route, dict):
            continue
        rpath = str(route.get("path") or "")
        if SENSITIVE_PATH.search(rpath):
            hit("no-sensitive-paths-exposed", here + ("path",), rpath)
        if not VERSIONED_PATH.match(rpath):
            hit("path-versioned", here + ("path",), rpath)
        limit = route.get("rate_limit")
        if not isinstance(limit, dict) or not limit.get("rps") or not limit.get("burst"):
            hit("rate-limit-required", here, rpath)
        if not route.get("max_body_bytes"):
            hit("request-size-capped", here, rpath)
        cors = route.get("cors")
        if isinstance(cors, dict) and "*" in (cors.get("allow_origins") or []) \
                and cors.get("allow_credentials") is True:
            hit("cors-no-wildcard-with-credentials", here + ("cors",), rpath)
    for name, up in (doc.get("upstreams") or {}).items():
        url = str((up or {}).get("url") or "") if isinstance(up, dict) else ""
        if not url.startswith("https://"):
            hit("upstream-https-required", ("upstreams", name, "url"), name)
    return sorted(found, key=lambda f: (f["line"], f["rule_id"]))


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print("usage: tenantlint.py <tenant.yaml> [--json]", file=sys.stderr)
        return 2
    path = args[0]
    try:
        with open(path, encoding="utf-8") as fh:
            doc, lines = parse(fh.read())
    except (OSError, Unsupported) as exc:
        print(f"tenantlint: {path}: {exc}", file=sys.stderr)
        return 2
    violations = check(doc, lines)
    if "--json" in argv:
        print(json.dumps({"path": path, "violations": violations}, indent=2))
    else:
        for v in violations:
            print(f"{path}:{v['line']}: [{v['category']}] {v['rule_id']}: {v['title']}")
        print(f"{len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
