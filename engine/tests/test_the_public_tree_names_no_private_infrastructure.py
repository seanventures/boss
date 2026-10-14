"""THE PUBLIC TREE NAMES NO PRIVATE HOST, NETWORK, PROJECT OR HOME DIRECTORY (operator rule).

BOSS is dogfooded on the owner's own machines. Incident notes, fixtures and test docstrings kept
picking up the names of those machines: the self-hosted server, its tailnet address and name, the
private work tracker's project id, a contributor's home directory. None of that belongs in a
public repository. Fixtures use neutral stand-ins instead: "the shared server", boss.tail1234.ts.net,
192.0.2.x / 198.51.100.x / 203.0.113.x (RFC 5737), 100.64.0.x (RFC 6598), plane.example.com.

The denylist holds SHA-256 digests of lowercase identifiers, never the identifiers themselves, so
this file cannot reintroduce what it guards against. Each tracked text file is cut four ways and
every piece is hashed and compared:

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
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 2_000_000

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

# Third-party data shipped verbatim (the Public Suffix List names every hosting suffix there is).
EXEMPT = {"engine/boss_engine/data/public_suffix_list.dat"}

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


def _tracked() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p.decode() for p in out.split(b"\0") if p]


def test_the_scanner_catches_each_shape_without_naming_it(monkeypatch):
    # The neutral stand-ins the fixtures use are not on the denylist.
    assert findings("deploy to boss.tail1234.ts.net from 192.0.2.202 at /Users/me/x") == []
    assert findings("https://boss.example.test and plane.example.com") == []
    # Each table, proven with a neutral word swapped in for the real ones.
    here = globals()
    for table, entry, text, want in [
        ("WORDS", "kestrelbox", "deploy to Kestrelbox failed", "w"),
        ("DOTTED", "corp.example", "https://plane.int.corp.example/api", "d"),
        ("DOTTED", "198.51.100.77", "ssh root@198.51.100.77:22", "d"),
        ("PAIRS", "work/acme", "cd ~/work/acme/boss-wt", "p"),
        ("HOME_USERS", "pat", "D = '/Users/pat/Downloads'", "h"),
        ("EMAILS", "pat@example.org", "mail Pat@Example.org", "e"),
    ]:
        monkeypatch.setitem(here, table, {_h(entry): want})
        assert findings(text) == [want], (table, text)
        monkeypatch.undo()
    monkeypatch.setitem(here, "SCOPED", {_h("example.org"): {_h("www.example.org")}})
    assert findings("https://www.example.org/install.sh") == []
    assert findings("https://ops.example.org") == ["unpublished name under the owner's domain"]
    monkeypatch.undo()
    for table in (WORDS, DOTTED, PAIRS, HOME_USERS, EMAILS, SCOPED, *SCOPED.values()):
        assert all(re.fullmatch(r"[0-9a-f]{64}", k) for k in table), "store digests, never words"


def test_no_tracked_file_names_private_infrastructure():
    hits = []
    for rel in _tracked():
        if rel in EXEMPT:
            continue
        cats = findings(rel)
        if cats:
            hits.append(f"{rel} (path): {', '.join(cats)}")
            continue
        path = ROOT / rel
        try:
            if path.is_symlink() or path.stat().st_size > MAX_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        cats = findings(data.decode("utf-8", errors="ignore"))
        if cats:
            hits.append(f"{rel}: {', '.join(cats)}")
    assert not hits, "tracked files name private infrastructure:\n  " + "\n  ".join(hits)
