"""THE PRIVATE-INFRASTRUCTURE DENYLIST: one list, for the tree and for anything leaving it.

The P0 test `engine/tests/test_the_public_tree_names_no_private_infrastructure.py` runs it over
every tracked file; BOSS's upstream path (services/self_fix_upstream.py, BOSSAPP-571 spec §7.3)
runs the SAME list over the exact bytes a person is about to send to the BOSS team — the diff, the
title, the body, the evidence, the trailers — and blocks the send on any hit. Two copies would
drift; this is the one.

BOSS is dogfooded on the owner's own machines. Incident notes, fixtures and test docstrings kept
picking up the names of those machines: the self-hosted server, its tailnet address and name, the
private work tracker's project id, a contributor's home directory. None of that belongs in a
public repository. Fixtures use neutral stand-ins instead: "the shared server", boss.tail1234.ts.net,
192.0.2.x / 198.51.100.x / 203.0.113.x (RFC 5737), 100.64.0.x (RFC 6598), plane.example.com.

The denylist holds SHA-256 digests of lowercase identifiers, never the identifiers themselves, so
this file cannot reintroduce what it guards against. Text is cut four ways and every piece is
hashed and compared:

  words      runs of [a-z0-9]                       a host name, a project-id fragment, a tailnet
  dotted     a.b.c names and IPv4 addresses, and    a private domain under any sub-host, an
             every dot-suffix of them               address of a real machine
  pairs      a/b path segment pairs, and the user   a private repository, a local checkout path,
             after /Users/ or /home/                a personal home directory
  emails     whole addresses                        a personal mailbox

One domain is scoped instead of banned: the product's public website lives under the owner's
domain, so a name under that parent passes only when it is one of the published names.

To add an identifier: hashlib.sha256(word.lower().encode()).hexdigest(), into the right table
with a category (shown on failure instead of the word). Never paste the word into this file.
This file is `never`-protected for self-fixes (packages/specpack/policies/self_fix_paths.yaml).
"""
from __future__ import annotations

import hashlib
import re

# A word anywhere in a tracked text file.
WORDS = {
    "a3918628a8f2821ae8abe7bbe1d817241df2cb93dccd4cfae83b2d8c64107c43": "private host name",
    "f6ded9c3ac807e0d6938e7fc8edca7c38a75f90fe415dbea48adf06c02d5c267": "private tracker project id",
    "a72e65b157d96cb1d6dd577ae068c4861b25ad177de10f4b843cfbb0a8a9b3c1": "private tracker project id",
    "b51b22062656794dd7d6de5287ccd709400a95a77e223163efc3f6147559ae28": "private tailnet name",
    "1994acb0c6f0473dbfb7cad2c6736c42edc8bbf03adee2772ea60a6ede7e859a": "private disk / local path",
    "8a15acc72b4b25c29c39e90080e1b48cf646e7aa293d940437b37c2de6f77b12": "owner's personal name",
}
# A dotted name or IPv4 address, or any dot-suffix of one.
DOTTED = {
    "6c7bc7228f625ce41d3fe8061935564e0cdebeea598d54cc7de54944687a8f4c": "private service domain",
    "72e0205e12697f47f280f7ec2b8bb2a83eb72daf92fac5596c291a45b3e4189a": "tailnet address of a real machine",
    "6812248b60d4176360628c01d31566f24bed8839093d3b7ab4b174e38d16c883": "public address of a real server",
}
# Parent domain -> the only names under it that may appear (the published website).
SCOPED = {
    "57d99050d6b37a4992a935a0b2b80335f775a306558f54bc82a4eb314d60bba9": {
        "57d99050d6b37a4992a935a0b2b80335f775a306558f54bc82a4eb314d60bba9",
        "907d18ab1c522a6a49c13e1d0bb0d7cb32d7cbd7dba8ffa1ca78ad7c8066d36d",
    },
}
# An "a/b" path pair.
PAIRS = {
    "30e4f98761738790197a25d6c11fdea351ab2e96b394756da756f2ebee6abc05": "local checkout path",
    "5fcd56a7cb83d9679c9b46ea0dbd98fbabe05e3169d5feea2a0742bb489e8940": "private repository",
}
# The user name after /Users/ or /home/.
HOME_USERS = {
    "2d1d92b1f4cc7fd3ef215da88c2826ab3ec2ced95b388b395b658c41b349c90e": "personal home directory",
    "ccaaac7c8b56412a1c396cf617a57930f8cda8da41aaf8382012ecd6380b16b2": "personal home directory",
    "8a15acc72b4b25c29c39e90080e1b48cf646e7aa293d940437b37c2de6f77b12": "personal home directory",
}
EMAILS = {
    "0055e0ff6d1d3dffedf6cb9e717f110feb252faec4b9094fe7f83b3b290b8b17": "personal email address",
}

WORD_RE = re.compile(r"[a-z0-9]+")
DOTTED_RE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")
PATH_RE = re.compile(r"[a-z0-9._~-]+(?:/[a-z0-9._~-]+)+")
HOME_RE = re.compile(r"/(?:users|home)/([a-z0-9._-]+)")
EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}")


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def findings(text: str) -> list[str]:
    """Categories of private identifiers in `text` (never the identifiers themselves)."""
    low = text.lower()
    found: list[str] = []
    for w in set(WORD_RE.findall(low)):
        if (cat := WORDS.get(_h(w))):
            found.append(cat)
    for name in set(DOTTED_RE.findall(low)):
        name = name.strip(".-")
        labels = name.split(".")
        for i in range(len(labels) - 1):
            suffix = ".".join(labels[i:])
            hs = _h(suffix)
            if (cat := DOTTED.get(hs)):
                found.append(cat)
            if hs in SCOPED and _h(name) not in SCOPED[hs]:
                found.append("unpublished name under the owner's domain")
    for path in set(PATH_RE.findall(low)):
        segments = path.split("/")
        for a, b in zip(segments, segments[1:]):
            if (cat := PAIRS.get(_h(f"{a}/{b}"))):
                found.append(cat)
    for user in set(HOME_RE.findall(low)):
        if (cat := HOME_USERS.get(_h(user))):
            found.append(cat)
    for email in set(EMAIL_RE.findall(low)):
        if (cat := EMAILS.get(_h(email))):
            found.append(cat)
    return sorted(set(found))


__all__ = ["DOTTED", "EMAILS", "HOME_USERS", "PAIRS", "SCOPED", "WORDS", "findings"]
