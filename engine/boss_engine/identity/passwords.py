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
from typing import Iterable, Optional

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
MIN_LENGTH = 12
MAX_LENGTH = 128


class PasswordBusy(Exception):
    """Too many hashes in flight for 2 s: the caller answers 429 (A13)."""


class PasswordRejected(Exception):
    """A new password breaks a rule. `code` is the stable error string; never echoes the input."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_HASHES)


def current_profile() -> Argon2Profile:
    return PROFILES.get((get_settings().argon2_profile or "default").strip().lower(),
                        PROFILES["default"])


def normalize(password: str) -> str:
    """Unicode NFKC, no truncation, no other changes (NIST 800-63B)."""
    return unicodedata.normalize("NFKC", password or "")


def _guarded(fn):
    def run(*args, **kwargs):
        if not _SEMAPHORE.acquire(timeout=QUEUE_SECONDS):
            raise PasswordBusy()
        try:
            return fn(*args, **kwargs)
        finally:
            _SEMAPHORE.release()
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
                       extra_words: Iterable[str] = ()) -> str:
    """Return the normalised password, or raise PasswordRejected (`password_too_short`,
    `password_too_long`, `password_too_common`). No composition rules (NIST 800-63B)."""
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
