"""PII sanitizer — redacts sensitive values before content crosses a data boundary to an external
model or tool (spec §7.4, §25). Pattern-based and deterministic so it's safe to run in the hot
path; returns the redacted text plus a summary of what was masked (for the audit trail).

Apply this only to data-boundary-sensitive content (e.g. customer data on a cloud task) — NOT to
code the factory is writing, where masking would corrupt the change. Callers opt in explicitly.
"""
from __future__ import annotations

import re

# CREDENTIALS, one class: every token shape a provider issues, and every place a secret is
# written as text (an assignment, a header, a PEM block, a JWT). Most specific first so a
# token is never half-masked. These run before the PII patterns and are also usable alone
# (``redact_secrets``) where PII masking would mangle the text (ids, timestamps, titles).
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("PRIVATE_KEY", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
        re.DOTALL)),
    ("SECRET", re.compile(r"\bsk-(?:ant|proj|svcacct|admin)-[A-Za-z0-9_\-]{8,}")),
    ("SECRET", re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test)?[-_]?[A-Za-z0-9]{16,}[A-Za-z0-9_\-]*")),
    ("SECRET", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{8,}")),
    ("SECRET", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("SECRET", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("SECRET", re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}")),
    ("SECRET", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    # A connection URL's password: scheme://user:PASSWORD@host (DATABASE_URL and friends).
    ("SECRET", re.compile(r"(?<=://)(?P<name>[^\s:/@'\"]{1,128})(?P<sep>:)(?P<value>[^\s@/'\"]{1,256})(?=@)")),
    ("BEARER", re.compile(r"\b(?:Bearer|Basic|token)\s+[A-Za-z0-9._~+/\-]{16,}=*", re.IGNORECASE)),
    # NAME_KEY=value, "password": "value", export API_TOKEN=value. The name is kept (it says
    # what was there); the value goes. A name must END in the secret word, so
    # `max_total_tokens_per_call=64000` and `key=code_agent_max_turns` are left alone.
    ("SECRET", re.compile(
        r"""(?ix)
        (?P<name>(?<![a-z0-9_.\-])[a-z0-9_.\-]{0,64}?
            (?:api[_\-]?key|access[_\-]?key|secret[_\-]?key|private[_\-]?key|[_\-]key
              |token|secret|password|passwd|pwd|credentials?|client[_\-]?secret))
        (?P<sep>["']?\s*[:=]\s*["']?)
        (?P<value>(?!\[)[^\s"'&,;)}\]]{4,})""")),
]

# (label, compiled pattern). Order matters: most-specific first so a token isn't half-masked.
_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("PHONE", re.compile(r"\b\+?\d[\d().\-\s]{8,}\d\b")),
]
_PATTERNS = _SECRET_PATTERNS + _PII_PATTERNS


def _apply(text: str, patterns: list[tuple[str, re.Pattern]]) -> tuple[str, dict[str, int]]:
    if not text:
        return text, {}
    summary: dict[str, int] = {}
    # Values BOSS itself handed a run (build_services: a run's database password): known exactly,
    # so removed exactly, whatever shape the output printed them in.
    from .build_services import scrub as _scrub_live

    out = _scrub_live(text)
    # BOSSAPP-866: every secret value resolved for a run (run_secret_redaction), with its
    # base64 and URL-encoded forms, removed exactly. A no-op while nothing is registered.
    from .run_secret_redaction import scrub as _scrub_known

    out = _scrub_known(out)
    if out != text:
        summary["SECRET"] = 1
    for label, pattern in patterns:
        count = 0

        def _sub(m, _label=label):
            nonlocal count
            count += 1
            if "value" in m.re.groupindex:  # an assignment keeps its name
                return f"{m.group('name')}{m.group('sep')}[{_label}]"
            return f"[{_label}]"

        out = pattern.sub(_sub, out)
        if count:
            summary[label] = summary.get(label, 0) + count
    return out, summary


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return (redacted_text, {label: count}). Each match becomes ``[LABEL]``: credentials
    first, then PII."""
    return _apply(text, _PATTERNS)


def redact_secrets(text: str) -> str:
    """Credentials only — for text PII masking would mangle (ids, times, titles, a CSV line)."""
    return _apply(text, _SECRET_PATTERNS)[0] if text else text


def has_pii(text: str) -> bool:
    return bool(redact(text)[1])


# ── URL credentials: one masker for every read view (BOSSAPP-1202, BOSSAPP-1203) ────────────
#
# `scheme://user:PASSWORD@host` keeps the user and the host and loses the password:
# `https://user:***@host/x`, `https://x-access-token:***@github.com/o/r`. A userinfo with no
# password whose user is itself a token (`https://ghp_…@github.com`) loses the user instead:
# `https://***@github.com`. A long service-account name (`deploy-bot-prod-account-01`) is a
# username and stays. The same password is masked when the userinfo is percent-encoded inside a
# query value, and a token query or fragment param (`access_token`, `sig`, `X-Amz-Signature`)
# loses its value. Not a URL, so never touched: an scp-style remote (`git@github.com:o/r.git`)
# and an email address. The password runs to the LAST `@` before the path, so `user:p@ss@host`
# is masked whole; a percent-encoded one inside userinfo (`p%40ss`) is just text.
URL_SECRET_MASK = "***"
_URL_USERINFO = re.compile(
    r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.\-]{0,31}://)"
    r"(?P<user>[^\s:/?#@\"'<>\\]*)"
    r"(?::(?P<password>[^\s/?#\"'<>\\]*))?"
    r"@(?=[^\s/?#@\"'<>\\]*(?:[/?#\s\"'<>\\]|$))")
_TOKEN_PREFIX = re.compile(
    r"^(?:gh[pousr]_|github_pat_|glpat-|xox[abposr]-|sk-|AKIA|ASIA|eyJ)")
#: Letters AND digits, 24+ chars, no word breaks: a 40-hex classic token, a JWT-ish blob.
_OPAQUE_USER = re.compile(
    r"^(?=[A-Za-z0-9._~%+\-]*[0-9])(?=[A-Za-z0-9._~%+\-]*[A-Za-z])[A-Za-z0-9._~%+\-]{24,}$")
#: `deploy`, `account` — a separator-bounded word, so the opaque rule does not eat a username.
_NAME_WORD = re.compile(r"(?:^|[._\-])[A-Za-z]{3,}(?=[._\-]|$)")
_ENCODED_USERINFO = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]{0,31})(?P<enc_scheme>%3[Aa]%2[Ff]%2[Ff])"
    r"(?P<user>(?:(?!%3[Aa]|%40)(?:%[0-9A-Fa-f]{2}|[A-Za-z0-9._~+\-]))*)"
    r"(?:(?P<enc_colon>%3[Aa])(?P<password>(?:(?!%40)(?:%[0-9A-Fa-f]{2}|[A-Za-z0-9._~+\-]))*))?"
    r"(?P<enc_at>%40)")
_TOKEN_PARAM = re.compile(
    r"(?ix)^(?:access[_\-]?token|refresh[_\-]?token|id[_\-]?token|api[_\-]?token"
    r"|session[_\-]?token|security[_\-]?token|auth[_\-]?token|api[_\-]?key|apikey"
    r"|access[_\-]?key|client[_\-]?secret|secret|password|passwd|pwd|credential"
    r"|signature|sig|token|key"
    r"|.*[_\-](?:token|signature|secret|password|credential|sig|access[_\-]?key|secret[_\-]?key))$")
_QUERY_SECRET = re.compile(
    r"(?P<lead>[?&#])(?P<name>[A-Za-z][A-Za-z0-9_.\-]*)=(?P<value>[^&#\s\"'<>\\]+)")


def _user_is_secret(user: str) -> bool:
    if not user:
        return False
    if _TOKEN_PREFIX.match(user):
        return True
    return bool(_OPAQUE_USER.match(user) and not _NAME_WORD.search(user))


def _mask_userinfo(m: "re.Match[str]") -> str:
    user, password = m.group("user"), m.group("password")
    if password is not None and password != "":
        return f"{m.group('scheme')}{user}:{URL_SECRET_MASK}@"
    if password is None and _user_is_secret(user):
        return f"{m.group('scheme')}{URL_SECRET_MASK}@"
    return m.group(0)


def _mask_encoded_userinfo(m: "re.Match[str]") -> str:
    from urllib.parse import unquote

    user = unquote(m.group("user"))
    password = unquote(m.group("password")) if m.group("password") is not None else None
    if password is not None and password != "":
        return (f"{m.group('scheme')}{m.group('enc_scheme')}{m.group('user')}"
                f"{m.group('enc_colon')}{URL_SECRET_MASK}{m.group('enc_at')}")
    if password is None and _user_is_secret(user):
        return f"{m.group('scheme')}{m.group('enc_scheme')}{URL_SECRET_MASK}{m.group('enc_at')}"
    return m.group(0)


def _mask_query_secret(m: "re.Match[str]") -> str:
    if not _TOKEN_PARAM.match(m.group("name")):
        return m.group(0)
    return f"{m.group('lead')}{m.group('name')}={URL_SECRET_MASK}"


def mask_url_credentials(text: str) -> str:
    """`text` with URL credentials replaced by `***`: userinfo (raw or percent-encoded) and
    token query/fragment params. Anywhere in the string. Everything else is unchanged."""
    if not text:
        return text
    encoded = "%3a%2f%2f" in text.lower()
    if "://" not in text and not encoded:
        return text
    if "@" in text and "://" in text:
        text = _URL_USERINFO.sub(_mask_userinfo, text)
    if encoded:
        text = _ENCODED_USERINFO.sub(_mask_encoded_userinfo, text)
    if any(mark in text for mark in ("?", "&", "#")):
        text = _QUERY_SECRET.sub(_mask_query_secret, text)
    return text


def mask_url_credentials_in(value, *, _depth: int = 0):
    """A copy of `value` with `mask_url_credentials` applied to every string at any depth
    (dict values AND keys, lists, tuples). For READ VIEWS: what an agent receives is not
    passed through it."""
    if _depth > 64:
        return value
    if isinstance(value, str):
        return mask_url_credentials(value)
    if isinstance(value, dict):
        return {(mask_url_credentials(k) if isinstance(k, str) else k):
                mask_url_credentials_in(v, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_url_credentials_in(v, _depth=_depth + 1) for v in value]
    return value
