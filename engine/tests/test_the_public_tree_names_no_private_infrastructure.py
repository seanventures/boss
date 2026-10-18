"""THE PUBLIC TREE NAMES NO PRIVATE HOST, NETWORK, PROJECT OR HOME DIRECTORY (operator rule).

BOSS is dogfooded on the owner's own machines. Incident notes, fixtures and test docstrings kept
picking up the names of those machines: the self-hosted server, its tailnet address and name, the
private work tracker's project id, a contributor's home directory. None of that belongs in a
public repository. Fixtures use neutral stand-ins instead: "the shared server", boss.tail1234.ts.net,
192.0.2.x / 198.51.100.x / 203.0.113.x (RFC 5737), 100.64.0.x (RFC 6598), plane.example.com.

The denylist (boss_engine/services/private_infra.py) holds SHA-256 digests of lowercase identifiers,
never the identifiers themselves, so
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

import re
import subprocess
from pathlib import Path

# THE TABLES live in boss_engine/services/private_infra.py: one denylist, run here over the tree
# and by the self-fix upstream path over everything a person sends to the BOSS team.
from boss_engine.services import private_infra
from boss_engine.services.private_infra import (DOTTED, EMAILS, HOME_USERS, PAIRS, SCOPED,
                                                WORDS, _h, findings)

ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 2_000_000

# Third-party data shipped verbatim (the Public Suffix List names every hosting suffix there is).
EXEMPT = {"engine/boss_engine/data/public_suffix_list.dat"}


def _tracked() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p.decode() for p in out.split(b"\0") if p]


def test_the_scanner_catches_each_shape_without_naming_it(monkeypatch):
    # The neutral stand-ins the fixtures use are not on the denylist.
    assert findings("deploy to boss.tail1234.ts.net from 192.0.2.202 at /Users/me/x") == []
    assert findings("https://boss.example.test and plane.example.com") == []
    # Each table, proven with a neutral word swapped in for the real ones.
    for table, entry, text, want in [
        ("WORDS", "kestrelbox", "deploy to Kestrelbox failed", "w"),
        ("DOTTED", "corp.example", "https://plane.int.corp.example/api", "d"),
        ("DOTTED", "198.51.100.77", "ssh root@198.51.100.77:22", "d"),
        ("PAIRS", "work/acme", "cd ~/work/acme/boss-wt", "p"),
        ("HOME_USERS", "pat", "D = '/Users/pat/Downloads'", "h"),
        ("EMAILS", "pat@example.org", "mail Pat@Example.org", "e"),
    ]:
        monkeypatch.setattr(private_infra, table, {_h(entry): want})
        assert findings(text) == [want], (table, text)
        monkeypatch.undo()
    monkeypatch.setattr(private_infra, "SCOPED", {_h("example.org"): {_h("www.example.org")}})
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
