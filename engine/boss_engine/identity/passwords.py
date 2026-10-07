"""Passwords: argon2id with upgradable profiles, a memory-DoS semaphore, NIST 800-63B rules, an
offline blocklist and an optional HIBP k-anonymity check (IDENTITY.md §4.2, D18).

argon2id comes from `cryptography` (already a dependency), stored as a PHC string:
    $argon2id$v=19$m=65536,t=3,p=1$<salt b64>$<tag b64>
A stored hash whose parameters are below the current profile is rehashed on the next successful
sign-in (`needs_rehash`). There is no pepper (§4.2).
"""
from __future__ import annotations

import gzip
import hashlib
import secrets
import threading
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Iterable, Optional

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from ..config import get_settings


@dataclass(frozen=True)
class Argon2Profile:
    name: str
    memory_kib: int
    iterations: int
    lanes: int = 1
    salt_bytes: int = 16
    tag_bytes: int = 32


PROFILES = {
    # RFC 9106's second recommended profile; 60–120 ms on a 2-vCPU VM.
    "default": Argon2Profile("default", memory_kib=65536, iterations=3),
    # The OWASP minimum, for hosts under 2 GiB of RAM (BOSS_ARGON2_PROFILE=small).
    "small": Argon2Profile("small", memory_kib=19456, iterations=2),
}
#: Recovery codes and email codes are hashed with the small profile (§4.7, §4.14).
CODE_PROFILE = PROFILES["small"]

MAX_CONCURRENT_HASHES = 4
QUEUE_SECONDS = 2.0
#: L2: how many hashes one client (IP) may have running or waiting at once. Over its share a
#: request is refused at once (429), so one address can never fill the queue and starve others.
PER_CLIENT_SHARE = 2
MIN_LENGTH = 12
MAX_LENGTH = 128


class PasswordBusy(Exception):
    """Too many hashes in flight for 2 s: the caller answers 429 (A13)."""


class PasswordRejected(Exception):
    """A new password breaks a rule. `code` is the stable error string; never echoes the input."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class FairHashQueue:
    """THE argon2 gate of this process (L2): at most `slots` hashes run at once; waiters are served
    strictly first-come first-served; each client (the request's IP) holds at most `per_client`
    places, running or waiting, and is refused immediately beyond that. A client of None (no
    request: CLI, migrations) has no share limit but still queues in order."""

    def __init__(self, slots: int, per_client: int) -> None:
        import collections

        self.slots = slots
        self.per_client = per_client
        self._cv = threading.Condition()
        self._running = 0
        self._held: dict[str, int] = collections.Counter()
        self._waiting: "collections.deque[object]" = collections.deque()

    def acquire(self, client: Optional[str], timeout: float) -> bool:
        import time

        with self._cv:
            if client is not None and self._held[client] >= self.per_client:
                return False
            ticket = object()
            self._waiting.append(ticket)
            if client is not None:
                self._held[client] += 1
            deadline = time.monotonic() + timeout
            while True:
                if self._running < self.slots and self._waiting[0] is ticket:
                    self._waiting.popleft()
                    self._running += 1
                    self._cv.notify_all()
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._waiting.remove(ticket)
                    if client is not None:
                        self._release_share(client)
                    self._cv.notify_all()
                    return False
                self._cv.wait(remaining)

    def release(self, client: Optional[str]) -> None:
        with self._cv:
            self._running -= 1
            if client is not None:
                self._release_share(client)
            self._cv.notify_all()

    def _release_share(self, client: str) -> None:
        self._held[client] -= 1
        if self._held[client] <= 0:
            del self._held[client]


_QUEUE = FairHashQueue(MAX_CONCURRENT_HASHES, PER_CLIENT_SHARE)

import contextvars  # noqa: E402

#: The client (IP) whose request is hashing now; set by the sign-in routes (hashing_for).
_CLIENT: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("boss_hash_client", default=None)


class hashing_for:
    """`with hashing_for(ip):` — hashes in this block count against that client's share."""

    def __init__(self, client: Optional[str]) -> None:
        self._client = client or "unknown"
        self._token: Any = None

    def __enter__(self) -> "hashing_for":
        self._token = _CLIENT.set(self._client)
        return self

    def __exit__(self, *exc: Any) -> None:
        _CLIENT.reset(self._token)


def current_profile() -> Argon2Profile:
    return PROFILES.get((get_settings().argon2_profile or "default").strip().lower(),
                        PROFILES["default"])


def normalize(password: str) -> str:
    """Unicode NFKC, no truncation, no other changes (NIST 800-63B)."""
    return unicodedata.normalize("NFKC", password or "")


def _guarded(fn):
    def run(*args, **kwargs):
        client = _CLIENT.get()
        if not _QUEUE.acquire(client, QUEUE_SECONDS):
            raise PasswordBusy()
        try:
            return fn(*args, **kwargs)
        finally:
            _QUEUE.release(client)
    return run


@_guarded
def _derive(secret: bytes, profile: Argon2Profile) -> str:
    kdf = Argon2id(salt=secrets.token_bytes(profile.salt_bytes), length=profile.tag_bytes,
                   iterations=profile.iterations, lanes=profile.lanes,
                   memory_cost=profile.memory_kib)
    return kdf.derive_phc_encoded(secret)


@_guarded
def _verify(secret: bytes, phc: str) -> bool:
    try:
        Argon2id.verify_phc_encoded(secret, phc)
        return True
    except (InvalidKey, ValueError, TypeError):
        return False


def hash_password(password: str, *, profile: Optional[Argon2Profile] = None) -> str:
    """PHC argon2id of the NFKC-normalised password. Raises PasswordBusy under memory pressure."""
    return _derive(normalize(password).encode("utf-8"), profile or current_profile())


def hash_code(code: str) -> str:
    """argon2id (small profile) of a short code: recovery codes, email codes."""
    return _derive(code.strip().upper().encode("utf-8"), CODE_PROFILE)


def verify_code(code: str, phc: str) -> bool:
    return _verify(code.strip().upper().encode("utf-8"), phc)


def verify_password(password: str, phc: Optional[str]) -> bool:
    """Constant work whether or not `phc` exists: with no stored hash a dummy verification runs,
    so an unknown user takes as long as a wrong password (A3)."""
    secret = normalize(password).encode("utf-8")
    if not phc:
        _verify(secret, _dummy_hash())
        return False
    return _verify(secret, phc)


@lru_cache(maxsize=4)
def _dummy_hash_for(profile_name: str) -> str:
    return _derive(secrets.token_bytes(16), PROFILES[profile_name])


def _dummy_hash() -> str:
    return _dummy_hash_for(current_profile().name)


def parse_params(phc: str) -> Optional[tuple[int, int, int]]:
    """(m, t, p) of a PHC argon2id string, or None when it is not one."""
    parts = (phc or "").split("$")
    if len(parts) < 6 or parts[1] != "argon2id":
        return None
    try:
        params = dict(item.split("=", 1) for item in parts[3].split(","))
        return int(params["m"]), int(params["t"]), int(params["p"])
    except (KeyError, ValueError):
        return None


def needs_rehash(phc: Optional[str], profile: Optional[Argon2Profile] = None) -> bool:
    """True when the stored parameters are weaker than the current profile (upgrade on sign-in)."""
    if not phc:
        return False
    params = parse_params(phc)
    if params is None:
        return True
    profile = profile or current_profile()
    m, t, _p = params
    return m < profile.memory_kib or t < profile.iterations


# ---- rules for a NEW password ---------------------------------------------------------------------

@lru_cache(maxsize=1)
def _blocklist() -> frozenset[str]:
    """The bundled list of common passwords (NFKC, lowercased; entries of 8+ characters from the
    UK NCSC 100k list). Offline: a self-hosted install is never without its floor."""
    data = resources.files("boss_engine.data").joinpath("common_passwords.txt.gz").read_bytes()
    return frozenset(line for line in gzip.decompress(data).decode("utf-8").splitlines() if line)


def _context_words(email: Optional[str], org_name: Optional[str]) -> list[str]:
    words = ["boss"]
    if email and "@" in email:
        local = email.split("@", 1)[0].lower()
        if len(local) >= 3:
            words.append(local)
    if org_name and len(org_name.strip()) >= 3:
        words.append(org_name.strip().lower())
    return words


def check_new_password(password: str, *, email: Optional[str] = None,
                       org_name: Optional[str] = None, min_length: int = MIN_LENGTH,
                       extra_words: Iterable[str] = (), policy: Any = None) -> str:
    """Return the normalised password, or raise PasswordRejected (`password_too_short`,
    `password_too_long`, `password_too_common`, `password_breached`). No composition rules
    (NIST 800-63B).

    `policy` is the org's IdentityPolicy: EVERY path that sets a password passes it (a test
    enforces it), so its minimum length and its optional HIBP breach check (§4.2) apply on all of
    them. HIBP unreachable: accepted, and posture warns."""
    if policy is not None:
        min_length = int(getattr(policy, "password_min_length", min_length) or min_length)
    value = _check_rules(password, email=email, org_name=org_name, min_length=min_length,
                         extra_words=extra_words)
    if policy is not None and getattr(policy, "hibp_check", False):
        result = hibp_check(value)
        if result.breached:
            raise PasswordRejected("password_breached")
        if not result.checked:
            note_hibp_unreachable()
    return value


#: When the optional breach check last could not reach HIBP (process-local; posture reads it).
_HIBP_UNREACHABLE_AT: Optional[float] = None


def note_hibp_unreachable() -> None:
    global _HIBP_UNREACHABLE_AT
    import time

    _HIBP_UNREACHABLE_AT = time.time()


def hibp_unreachable_since(seconds: int = 86400) -> bool:
    """Did a breach check fail to reach HIBP in the last `seconds`?"""
    import time

    return _HIBP_UNREACHABLE_AT is not None and time.time() - _HIBP_UNREACHABLE_AT < seconds


def _check_rules(password: str, *, email: Optional[str], org_name: Optional[str], min_length: int,
                 extra_words: Iterable[str]) -> str:
    value = normalize(password)
    if len(value) < max(8, min_length):
        raise PasswordRejected("password_too_short")
    if len(value) > MAX_LENGTH:
        raise PasswordRejected("password_too_long")
    lowered = value.lower()
    if lowered in _blocklist():
        raise PasswordRejected("password_too_common")
    for word in [*_context_words(email, org_name), *(w.lower() for w in extra_words if w)]:
        if word not in lowered:
            continue
        # A context word plus filler ("boss12345678", "alice2024alice", "boss" + a common word)
        # is as guessable as the word itself.
        rest = lowered.replace(word, "")
        letters = "".join(ch for ch in rest if ch.isalpha())
        if len(set(rest)) <= 3 or len(letters) < 4 or rest in _blocklist():
            raise PasswordRejected("password_too_common")
    return value


@dataclass(frozen=True)
class BreachCheck:
    breached: bool
    checked: bool          # False: HIBP unreachable; accepted, with a posture warning


def hibp_check(password: str) -> BreachCheck:
    """Optional HIBP range query (k-anonymity, padded) through the SSRF-safe client. Unreachable
    means "accepted, not checked" — the bundled blocklist is the floor (§4.2)."""
    from .http import OutboundRefused, fetch

    digest = hashlib.sha1(normalize(password).encode("utf-8")).hexdigest().upper()  # noqa: S324
    prefix, suffix = digest[:5], digest[5:]
    try:
        resp = fetch("GET", f"https://api.pwnedpasswords.com/range/{prefix}",
                     headers={"Add-Padding": "true"}, purpose="password.hibp")
    except OutboundRefused:
        return BreachCheck(False, False)
    if resp.status != 200:
        return BreachCheck(False, False)
    for line in resp.body.decode("utf-8", "replace").splitlines():
        candidate, _, count = line.partition(":")
        if candidate.strip().upper() == suffix and count.strip() not in ("", "0"):
            return BreachCheck(True, True)
    return BreachCheck(False, True)
