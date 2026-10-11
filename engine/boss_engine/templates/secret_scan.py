"""The secret scan (F2 §2.3 step 4, §2.5): one scanner for an import and for a build's diff.

A finding names the file, the line and the RULE that matched, never the matched text: the scan's
own report must not become the leak (§2.9 S2.6; memory: never print any part of a credential).

What it looks for:
  * common credential formats (cloud keys, Git forge and chat tokens, payment keys, private keys,
    JWTs, a password or token assigned a literal in code or config);
  * the template's own `pattern` and `live_pattern` for each secret it declares;
  * the exact values an app's secret bindings hold (passed in by the gate; never logged).

A placeholder is not a secret (except the rules in NEVER_A_PLACEHOLDER): `sk_test_...`, `<your key>`, `${STRIPE_SECRET_KEY}`, `changeme`
and values made of one repeated character are skipped, so a guide or an `.env.example` can say
what goes where.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

#: (rule, regex). Each rule is a stable name the report and the tests use.
COMMON_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY(?: BLOCK)?-----")),
    # AWS's documented example credentials. Public, but a template carrying them is shipping
    # something shaped like a live key that people copy into real config: always a finding,
    # never "a placeholder" (the word EXAMPLE in them would otherwise excuse them).
    ("aws_documented_example_key", re.compile(r"\bAKIAIOSFODNN7EXAMPLE\b|wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("github_fine_grained_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/T[A-Za-z0-9_]+/B[A-Za-z0-9_]+/[A-Za-z0-9_]+")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("stripe_webhook_secret", re.compile(r"\bwhsec_[A-Za-z0-9]{24,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("google_oauth_client_secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{32,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{32,}\b")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("sendgrid_key", re.compile(r"\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}\b")),
    ("twilio_key", re.compile(r"\bSK[0-9a-f]{32}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("url_with_password", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^/\s:@'\"]{1,64}:([^/\s@'\"]{6,})@[^\s'\"]+")),
    # `password = "hunter2hunter2"`, `API_TOKEN: 'abc…'`, `"client_secret": "…"`.
    ("assigned_secret", re.compile(
        r"(?i)(?:password|passwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|client[_-]?secret"
        r"|private[_-]?key|token)[\"']?\s*[:=]\s*[\"']([^\"'\s]{12,})[\"']")),
)

#: A value that says where a secret goes rather than being one.
_PLACEHOLDER = re.compile(
    r"(?i)^(?:<[^>]*>|\$\{[^}]*\}|\$[A-Z_][A-Z0-9_]*|\{\{[^}]*\}\}|%\([^)]*\)s|"
    r".*(?:\.\.\.|xxxx|your[_\- ]|example|changeme|placeholder|replace[_\- ]?me|dummy|redacted|"
    r"not[_\- ]a[_\- ]real|fake|sample).*)$")

#: Rules whose every match is a finding, whatever words the match contains.
NEVER_A_PLACEHOLDER = frozenset({"private_key", "aws_documented_example_key"})

#: Files the scan reads. Anything else (images, fonts) is skipped; the import's binary rule
#: already refuses unknown binaries.
MAX_SCAN_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str

    def as_dict(self) -> dict:
        return {"path": self.path, "line": self.line, "rule": self.rule}

    def reason(self) -> str:
        return f"{self.path}:{self.line}: looks like a committed secret ({self.rule})"


def _is_placeholder(value: str) -> bool:
    v = value.strip().strip("\"'")
    return (not v) or bool(_PLACEHOLDER.match(v)) or len(set(v)) <= 2


def _declared_rules(patterns: Iterable[tuple[str, str]]) -> list[tuple[str, re.Pattern[str]]]:
    """A manifest pattern is anchored (`^sk_test_…$`) to check a whole value; to find one inside
    a line the anchors come off."""
    out = []
    for name, raw in patterns:
        if not raw:
            continue
        body = raw[1:] if raw.startswith("^") else raw
        body = body[:-1] if body.endswith("$") and not body.endswith("\\$") else body
        try:
            out.append((name, re.compile(body)))
        except re.error:
            continue
    return out


def manifest_patterns(manifest) -> list[tuple[str, str]]:
    """(rule name, regex) for every secret a manifest declares a pattern for."""
    out = []
    for s in getattr(manifest, "secrets", ()) or ():
        if s.pattern:
            out.append((f"declared:{s.name}", s.pattern))
        if s.live_pattern:
            out.append((f"declared_live:{s.name}", s.live_pattern))
    return out


def scan_text(path: str, text: str, *, declared: Iterable[tuple[str, str]] = (),
              values: Optional[Mapping[str, str]] = None) -> list[Finding]:
    """Findings in one file. `values` maps a binding's name to its value (the gate's bound
    secrets); a value shorter than 8 characters is not searched for (it would match noise)."""
    findings: list[Finding] = []
    rules = list(COMMON_RULES) + _declared_rules(declared)
    exact = {name: v for name, v in (values or {}).items() if isinstance(v, str) and len(v) >= 8}
    for lineno, line in enumerate(text.splitlines(), start=1):
        if len(line) > 20000:
            line = line[:20000]
        seen: set[str] = set()
        for rule, rx in rules:
            for m in rx.finditer(line):
                value = m.group(m.lastindex) if m.lastindex else m.group(0)
                if rule in seen or (rule not in NEVER_A_PLACEHOLDER and _is_placeholder(value)):
                    continue
                seen.add(rule)
                findings.append(Finding(path, lineno, rule))
        for name, value in exact.items():
            if value in line:
                findings.append(Finding(path, lineno, f"bound_secret:{name}"))
    return findings


def scan_files(files: Mapping[str, bytes | str], *, declared: Iterable[tuple[str, str]] = (),
               values: Optional[Mapping[str, str]] = None) -> list[Finding]:
    """Scan a {path: content} tree (an import, or a build's changed files)."""
    declared = list(declared)
    out: list[Finding] = []
    for path in sorted(files):
        content = files[path]
        if isinstance(content, bytes):
            if len(content) > MAX_SCAN_BYTES or b"\x00" in content[:8192]:
                continue
            text = content.decode("utf-8", errors="replace")
        else:
            text = content
        out.extend(scan_text(path, text, declared=declared, values=values))
    return out
