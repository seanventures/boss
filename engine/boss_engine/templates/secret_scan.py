"""The secret scan (F2 §2.3 step 4, §2.5): one scanner for an import and for a build's diff.

A finding names the file, the line and the RULE that matched, never the matched text: the scan's
own report must not become the leak (§2.9 S2.6; memory: never print any part of a credential).

What it looks for:
  * common credential formats (cloud keys, Git forge and chat tokens, payment keys, private keys,
    JWTs, a password or token assigned a literal in code or config);
  * the template's own `pattern` and `live_pattern` for each secret it declares;
  * the exact values an app's secret bindings hold (passed in by the gate; never logged).

A DEVELOPMENT DEFAULT IS NOT A SECRET EITHER. A HelpDesk build was flagged for
`postgres://helpdesk:helpdesk@localhost:5432/helpdesk` in .env.example,
`postgres://postgres:postgres@db` in docker-compose.yml, `password: "Agent#Demo2024"` in its seed
and a demo password in its README. A connection URL to loopback or a compose service, or whose
password is the user or database name, is a placeholder; so is a generic literal that says it is
a demo value, and, in an example file or a document, one that says it is a password/secret/dev
value. These excuse only the GENERIC literal rules (TEST_LITERAL_RULES): a credential FORMAT
(ghp_, AKIA, sk_live_, a private key, a JWT) and a bound value always fire, in every file.

A placeholder is not a secret (except the rules in NEVER_A_PLACEHOLDER): `sk_test_...`, `<your key>`, `${STRIPE_SECRET_KEY}`, `changeme`
and values made of one repeated character are skipped, so a guide or an `.env.example` can say
what goes where.

A TEST'S OWN LITERAL IS NOT A COMMITTED SECRET. HelpDesk AI (crun_db538dff872549bf9359) was
blocked for "something that looks like a committed secret": helpdesk-ai/vitest.config.ts lines
14-15, `DEMO_PASSWORD` and `SESSION_SECRET` set for the test workers to values that say they are
test values. A file only a test run reads (is_test_only_path: test sources, fixtures and the test
runner's config) may assign a value that names itself a test value; the GENERIC literal rules
(TEST_LITERAL_RULES) skip it there. Every credential FORMAT rule (a ghp_ token, an AKIA key, a
private key) and every exact bound value still fire in a test file, and a literal that does not
say it is a test value is still a finding — fixture_secret_repair hands that one back to QA.
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

#: A word that marks a value as a seed or demo value, not a credential: `Agent#Demo2024` in a seed
#: file, a demo password in the README. Never consulted for a credential FORMAT rule.
_DEMO_WORD = re.compile(r"(?i)demo")

#: In an example or a document a generic literal illustrates: these words say the value is one
#: (`password: "admin123"`, `SECRET=dev-secret-please-change`).
_DOC_PLACEHOLDER_WORD = re.compile(r"(?i)demo|password|passw0rd|secret|admin|local|dev|test")

#: Hosts that are this machine, or a compose service (one label, no dots: `db`, `postgres`).
_LOOPBACK_HOSTS = frozenset({"localhost", "0.0.0.0", "::1", "[::1]", "host.docker.internal"})
_URL_PARTS = re.compile(
    r"^[a-z][a-z0-9+.\-]*://(?P<user>[^/\s:@'\"]{1,64}):(?P<pw>[^/\s@'\"]+)@"
    r"(?P<host>\[[^\]]+\]|[^/\s:'\"?#]+)(?::\d+)?(?:/(?P<db>[^/\s?#'\"]*))?")


def _local_url_placeholder(url: str) -> bool:
    """A connection URL to this machine or to a compose service, or whose password is just the
    user or database name (`postgres://helpdesk:helpdesk@localhost:5432/helpdesk`,
    `postgres://postgres:postgres@db`): a development default, not a committed credential."""
    m = _URL_PARTS.match(url or "")
    if not m:
        return False
    host = m.group("host").lower()
    pw = m.group("pw")
    if pw in {m.group("user"), m.group("db") or None}:
        return True
    return (host in _LOOPBACK_HOSTS or host.startswith("127.")
            or ("." not in host and not host.startswith("[")))


_DOC_SUFFIXES = (".md", ".mdx", ".markdown", ".rst", ".adoc", ".txt")
_EXAMPLE_NAME = re.compile(r"(?i)(?:^|\.)(?:example|sample|template|dist)(?:\.|$)")


def is_doc_or_example_path(path: str) -> bool:
    """A document or an example file: what a value there says is illustration, never config the
    app reads (`.env.example`, `config.sample.json`, README.md, docs/...)."""
    p = "/" + str(path).replace("\\", "/").lower().lstrip("/")
    base = p.rsplit("/", 1)[-1]
    return (base.endswith(_DOC_SUFFIXES) or "/docs/" in p or base.startswith("readme")
            or bool(_EXAMPLE_NAME.search(base)))


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


#: The generic "a secret-named thing was assigned a literal" rules: the ones a test's own fixture
#: value trips. Credential formats are never in this set.
TEST_LITERAL_RULES = frozenset({"assigned_secret", "url_with_password"})

#: A value that says it exists for tests.
_TEST_VALUE = re.compile(r"(?i)test|fixture|mock|stub|vitest|jest|e2e")

_TEST_DIRS = ("/tests/", "/test/", "/spec/", "/__tests__/", "/e2e/", "/fixtures/", "/__fixtures__/",
              "/__mocks__/", "/testdata/")
_TEST_SUFFIXES = (
    "_test.py", "_test.go", "_test.rs", "_test.rb", "_spec.rb",
    ".test.js", ".test.jsx", ".test.ts", ".test.tsx", ".test.mjs", ".test.cjs",
    ".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx", ".spec.mjs", ".spec.cjs",
    "test.java", "tests.java", "test.kt", "test.cs", "tests.cs")
#: A test runner's own configuration and setup: read by the test run, never by the app.
_TEST_RUNNER_FILE = re.compile(
    r"^(?:vitest|jest|playwright|cypress|karma|mocha)\.(?:config|setup|workspace)(?:\.[\w-]+)?\.(?:[cm]?[jt]s)$"
    r"|^\.mocharc\.\w+$|^setuptests\.[cm]?[jt]sx?$|^conftest\.py$|^pytest\.ini$|^\.env\.test(?:\.local)?$")


def is_test_only_path(path: str) -> bool:
    """A file only a test run reads: a test source, a fixture, or the test runner's config."""
    p = "/" + str(path).replace("\\", "/").lower().lstrip("/")
    base = p.rsplit("/", 1)[-1]
    if any(seg in p for seg in _TEST_DIRS):
        return True
    if base.startswith("test_") or base.endswith(_TEST_SUFFIXES):
        return True
    return bool(_TEST_RUNNER_FILE.match(base))


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
    test_only = is_test_only_path(path)
    doc_or_example = is_doc_or_example_path(path)
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
                if test_only and rule in TEST_LITERAL_RULES and _TEST_VALUE.search(value):
                    continue  # a test's own, self-declared test value (see the module doc)
                if rule == "url_with_password" and _local_url_placeholder(m.group(0)):
                    continue  # a local / compose development default (_local_url_placeholder)
                if rule in TEST_LITERAL_RULES and _DEMO_WORD.search(value):
                    continue  # a seed or demo value that says it is one
                if (doc_or_example and rule in TEST_LITERAL_RULES
                        and _DOC_PLACEHOLDER_WORD.search(value)):
                    continue  # an example's or a document's illustrative value
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
