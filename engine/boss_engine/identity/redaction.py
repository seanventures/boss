"""THE redaction filter (IDENTITY.md §8.3, A80). One implementation, used everywhere a value can
reach a log, an error report, an HTTP access log or the security event log. Owned by stream S5.

What it removes:
  - mapping keys that name a credential: Authorization, Cookie, Set-Cookie, X-Boss-CSRF,
    X-Boss-Api-Key, and any key whose words include token|secret|password|code|assertion|
    credential (a short, explicit allow-list keeps the public ids: `token_jti`, `credential_id`,
    `status_code`, ...);
  - values shaped like a BOSS credential (boss_pat_/boss_sak_/boss_scim_/boss_egr_ keys, bss_
    session tokens), a JWT (`eyJ….….…`), a `Bearer …`/`Basic …`/`DPoP …` header value, the
    `Cookie:`/`Authorization:` line of a dumped request, and credential-named query or form
    parameters (`?code=…&state=…`, `password=…`);
  - every literal secret the install is configured with (BOSS_API_KEY, BOSS_API_KEYS, the console
    password and session secret, the SCIM bearer tokens, the worker key): those have no shape to
    recognise, so they are scrubbed by value. Registered once from the settings, and by tests.

`install()` puts it at the logging root in two places, so nothing routes around it:
  - a LogRecord factory: every record, from every logger, is redacted the moment it is created
    (message, args element by element so formatters that unpack args still work, the exception
    text and the stack);
  - a filter on every handler that exists at install time and on the uvicorn loggers, which do
    not propagate to the root.
It is idempotent. `boss_engine.identity.audit` calls it on import, so any process that can write
a security event also redacts its logs.
"""
from __future__ import annotations

import logging
import re
import threading
import traceback
from typing import Any, Iterable, Mapping

REDACTED = "[REDACTED]"

#: Header names that are dropped whatever their value.
SENSITIVE_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-boss-csrf",
    "x-boss-api-key", "x-api-key", "x-boss-mcp-confirmation", "dpop",
})

#: A key is sensitive when one of its words is one of these.
_SENSITIVE_WORDS = ("token", "tokens", "secret", "secrets", "password", "passwords", "passwd",
                    "passphrase", "code", "codes", "assertion", "assertions", "credential",
                    "credentials", "apikey", "cookie", "cookies", "authorization", "otp",
                    "verifier", "pkce", "dek", "kek", "private_key", "privatekey",
                    "samlresponse", "samlrequest", "csrf", "xsrf")

#: Keys that contain a sensitive word but hold a public identifier or a number, never a secret.
#: Kept short and explicit on purpose: adding one is a reviewed change.
SAFE_KEYS = frozenset({
    "token_jti", "jti", "parent_jti", "credential_id", "credential_kind", "status_code",
    "exit_code", "error_code", "reason_code", "http_code", "code_task", "code_task_id",
    "token_type", "grant_type", "code_challenge_method", "country_code", "token_count",
    "tokens_in", "tokens_out", "input_tokens", "output_tokens", "total_tokens", "max_tokens",
    "token_endpoint", "credential_purpose", "secret_ref_id", "has_password", "password_set",
    "password_changed_at", "code_length",
})

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def is_sensitive_key(key: Any) -> bool:
    """True when a mapping key names a credential (and is not a known public id)."""
    if not isinstance(key, str):
        return False
    lowered = key.strip().lower().replace("-", "_")
    if lowered in SAFE_KEYS:
        return False
    if lowered in SENSITIVE_HEADERS or lowered.replace("_", "-") in SENSITIVE_HEADERS:
        return True
    # camelCase -> words too ("clientSecret", "accessToken")
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key.strip()).lower()
    words = [w for w in _WORD_SPLIT.split(spaced) if w]
    if any(w in _SENSITIVE_WORDS for w in words):
        return True
    joined = "".join(words)
    return any(s in joined for s in ("privatekey", "apikey", "accesskey", "clientsecret"))


# ---- value shapes --------------------------------------------------------------------------------

_SHAPES: list[tuple[re.Pattern[str], str]] = [
    # PEM blocks (private keys) first: a later pattern must not eat the BEGIN line and leave the
    # body. An unterminated block is cut to the end of the text.
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
                re.S), REDACTED),
    # BOSS credentials (IDENTITY.md §5.1; egress §7.10; sessions §4.3; a session's model-proxy
    # token, services/model_proxy.py). No prefix survives either.
    (re.compile(r"boss_(?:pat|sak|scim|egr|mdl)_[A-Za-z0-9]{6,}"), REDACTED),
    (re.compile(r"\bbss_[A-Za-z0-9_\-]{16,}"), REDACTED),
    # JWT / JWS / id_token shape.
    (re.compile(r"eyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{2,}\.[A-Za-z0-9_\-]*"), REDACTED),
    # Header lines of a dumped request or response.
    (re.compile(r"(?im)\b(authorization|proxy-authorization|cookie|set-cookie|x-boss-csrf|"
                r"x-boss-api-key|x-api-key|dpop)\s*:\s*[^\r\n]*"), r"\1: " + REDACTED),
    # Auth scheme values wherever they appear.
    (re.compile(r"(?i)\b(bearer|dpop)\s+[A-Za-z0-9._~+/=\-]{8,}"), r"\1 " + REDACTED),
    (re.compile(r"(?i)\b(basic)\s+[A-Za-z0-9+/]{12,}={0,2}"), r"\1 " + REDACTED),
    # Cookie pairs for the identity cookies, even without a header name.
    (re.compile(r"(__Host-boss_[a-z]+|boss_console_session)=[^;\s,&]+"), r"\1=" + REDACTED),
    # Query-string parameters of OAuth/SSO callbacks and links (?code=…&state=…, #token=…).
    (re.compile(r"(?i)([?&;#])((?:access_|refresh_|id_|api_|session_|setup_|reset_|client_)?"
                r"(?:token|code|state|secret|password|assertion|samlresponse|samlrequest|"
                r"code_verifier|credential|key|sig|signature))=([^&\s#\"']+)"),
     r"\1\2=" + REDACTED),
    # key=value / "key": "value" pairs in free text and JSON with a credential name.
    (re.compile(r"(?i)(^|[\s,{(\"'])((?:access_|refresh_|id_|api_|session_|setup_|reset_|"
                r"client_|console_)?(?:token|secret|password|passwd|assertion|credential|"
                r"api_key|code_verifier|csrf|xsrf|samlresponse|samlrequest|auth_code|"
                r"authorization_code|oauth_code|otp|totp))(\"?\s*[=:]\s*\"?)([^\s\"',;}&]+)"),
     r"\1\2\3" + REDACTED),
]

_literals_lock = threading.Lock()
_literals: tuple[str, ...] = ()
#: Configured secrets shorter than this are not scrubbed by value (they would shred ordinary
#: words); config validation elsewhere refuses keys that short.
MIN_LITERAL = 8


def register_secrets(values: Iterable[Any]) -> None:
    """Add literal secret values to scrub (configured keys and passwords). Never logged."""
    global _literals
    fresh = {str(v).strip() for v in values if v and len(str(v).strip()) >= MIN_LITERAL}
    with _literals_lock:
        merged = set(_literals) | fresh
        # longest first, so a key that contains another is removed whole
        _literals = tuple(sorted(merged, key=len, reverse=True))


def register_configured_secrets(settings: Any = None) -> None:
    """Register every secret the settings hold. Safe to call more than once."""
    try:
        if settings is None:
            from ..config import get_settings
            settings = get_settings()
    except Exception:  # noqa: BLE001 — redaction must never stop a process from starting
        return
    values: list[Any] = []
    for name in ("api_key_values", "enterprise_scim_bearer_token_values"):
        try:
            values.extend(getattr(settings, name) or ())
        except Exception:  # noqa: BLE001
            pass
    for name in ("legacy_console_api_key", "legacy_console_password", "identity_hmac_key",
                 "oidc_client_secret", "github_webhook_secret", "bitbucket_webhook_secret",
                 "webhook_inbound_secret", "infisical_client_secret", "github_app_private_key",
                 "google_oauth_client_secret"):
        value = getattr(settings, name, None)
        if value:
            values.append(value)
    for token, _member in (getattr(settings, "api_key_principal_map", {}) or {}).items():
        values.append(token)
    register_secrets(values)


def redact_text(text: str) -> str:
    """Redact one string: configured literals first, then every credential shape."""
    if not text:
        return text
    out = text
    for literal in _literals:
        if literal in out:
            out = out.replace(literal, REDACTED)
    for pattern, repl in _SHAPES:
        out = pattern.sub(repl, out)
    return out


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Redact any value: strings by shape and literal, mappings by key (and their values),
    sequences element by element. Other types pass through. Never raises."""
    if _depth > 20:
        return REDACTED
    try:
        if isinstance(value, str):
            return redact_text(value)
        if isinstance(value, bytes):
            return redact_text(value.decode("utf-8", "replace")).encode("utf-8")
        if isinstance(value, Mapping):
            return {k: (REDACTED if is_sensitive_key(k) else redact(v, _depth=_depth + 1))
                    for k, v in value.items()}
        if isinstance(value, tuple):
            return tuple(redact(v, _depth=_depth + 1) for v in value)
        if isinstance(value, list):
            return [redact(v, _depth=_depth + 1) for v in value]
        if isinstance(value, (set, frozenset)):
            return type(value)(redact(v, _depth=_depth + 1) for v in value)
        return value
    except Exception:  # noqa: BLE001 — a redaction failure must not leak the original
        return REDACTED


def sensitive_keys_in(value: Any, *, _path: str = "") -> list[str]:
    """Every key path in a nested mapping that names a credential (audit.record refuses them)."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for k, v in value.items():
            path = f"{_path}.{k}" if _path else str(k)
            if is_sensitive_key(k):
                found.append(path)
            found.extend(sensitive_keys_in(v, _path=path))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            found.extend(sensitive_keys_in(v, _path=f"{_path}[{i}]"))
    return found


# ---- logging -------------------------------------------------------------------------------------

def _redact_args(args: Any) -> Any:
    if isinstance(args, Mapping):
        return redact(dict(args))
    if isinstance(args, tuple):
        return tuple(redact(a) if isinstance(a, (str, bytes, Mapping, list, tuple)) else a
                     for a in args)
    return args


def _redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """Redact one record in place. The args keep their shape (uvicorn's access formatter unpacks
    them); the template is only rewritten when it, or the formatted whole, holds a credential."""
    if getattr(record, "_boss_redacted", False):
        return record
    try:
        if record.msg is not None and not isinstance(record.msg, (str, int, float)):
            record.msg = str(record.msg)
        if record.args:
            record.args = _redact_args(record.args)
        if isinstance(record.msg, str):
            if redact_text(record.msg) != record.msg:
                # the template itself names a credential ("password=%s"): format, then redact
                try:
                    formatted = record.msg % record.args if record.args else record.msg
                except Exception:  # noqa: BLE001
                    formatted = record.msg
                record.msg, record.args = redact_text(formatted), ()
            elif record.args:
                # a secret only visible once formatted ("%s%s" % halves)
                try:
                    formatted = record.msg % record.args
                except Exception:  # noqa: BLE001
                    formatted = None
                if formatted is not None and redact_text(formatted) != formatted:
                    record.msg, record.args = redact_text(formatted), ()
        if record.exc_info and not record.exc_text:
            record.exc_text = redact_text("".join(traceback.format_exception(*record.exc_info)))
        elif record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        record._boss_redacted = True  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        record.msg, record.args = REDACTED, ()
    return record


class RedactionFilter(logging.Filter):
    """Handler/logger filter form of the same redaction (for records made before install)."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        _redact_record(record)
        return True


_FILTER = RedactionFilter()
_installed = False
_install_lock = threading.Lock()
#: Loggers that do not propagate to the root by default (uvicorn's access log among them).
NAMED_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error", "gunicorn", "gunicorn.access",
                 "gunicorn.error", "httpx", "sqlalchemy.engine", "fastapi", "starlette")


def _attach_to_handlers() -> None:
    loggers = [logging.getLogger()] + [logging.getLogger(n) for n in NAMED_LOGGERS]
    for lg in loggers:
        if _FILTER not in lg.filters:
            lg.addFilter(_FILTER)
        for handler in lg.handlers:
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)


def install() -> None:
    """Install the filter at the logging root (record factory + handler filters). Idempotent;
    calling it again re-attaches to handlers added since (uvicorn configures logging late)."""
    global _installed
    with _install_lock:
        if not _installed:
            previous = logging.getLogRecordFactory()

            def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
                return _redact_record(previous(*args, **kwargs))

            factory._boss_redaction = True  # type: ignore[attr-defined]
            logging.setLogRecordFactory(factory)
            register_configured_secrets()
            _installed = True
        _attach_to_handlers()


def installed() -> bool:
    return _installed and getattr(logging.getLogRecordFactory(), "_boss_redaction", False)
