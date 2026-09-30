"""Naming a piece of work so a person recognises it.

Titles were built by truncating whatever prose started the brief:

    title = ("Web app - " + directive[:70])

which produced "Web app - # Product Requirements Document (PRD)   **Project:** Lightweight
Self-" for a project the PRD itself names on the very next line: "Shorty". Four different
benchmark apps all displayed as "QA benchmark", because the label came first and the name was
cut off. An operator cannot tell one card from another, which is the whole job of a title.

The document usually SAYS what it is called — `**Project:** X`, a first heading, a quoted
working title. This reads that, strips the wrappers BOSS adds ("Web app -", "Build", "· retry
2"), lifts run-kind prefixes into a label, and returns a name short enough to read at a glance
with the descriptor kept separately.
"""
import re
import unicodedata


#: A heading that names the DOCUMENT rather than the thing the document is about.
#:
#: The trailing group is why this is not a one-liner. The phrase is almost never written bare:
#: real PRDs are headed "Product Requirements Document (PRD)", and the pipeline's own markdown
#: arrives as "**Product Requirements Document (PRD)**". Anchoring on `$` right after the
#: phrase meant neither matched, so the document TYPE became the project name — two workflow
#: runs in the console called "Product Requirements Document (PRD)", one of them still wearing
#: its markdown asterisks.
BOILERPLATE = re.compile(
    r"^(product requirements document|prd|design|spec|specification|overview|summary|"
    r"web app|version\b.*|introduction|document)\b"
    # "(PRD)", "(draft)", "(v2)" — an acronym or qualifier restating the heading.
    r"(\s*\([^)]{0,24}\))?"
    r"[\s:–—*_#`-]*$", re.I)

def _unadorn(s: str) -> str:
    """Drop a leading emoji or symbol so the boilerplate test can see the words behind it.

    THE RENDERER FIXED THIS AND THIS FILE DID NOT, which made the same oversight far worse
    here. `BOILERPLATE` is anchored at ^, and the pipeline titles its PRD runs
    "📄 Product Requirements Document" — so the check never fired, the document TYPE became the
    app's name, and `stable_app_id` hashed it. Every app ever built from a PRD was therefore
    stamped with the SAME app_id, the console unioned them on that token, and clicking one app
    showed another's runs under a third one's name.

    In the renderer this is a wrong label. Here it is a wrong identity, written down.
    """
    adornment = "\u200d\ufe0f\u2022\u00b7*_>#`"   # ZWJ, variation selector, bullets, markdown

    def is_adornment(ch: str) -> bool:
        return (ch.isspace() or unicodedata.category(ch).startswith("S")
                or ch in adornment)

    i = 0
    while i < len(s) and is_adornment(s[i]):
        i += 1
    # AND FROM THE END. Stripping only the front left "**Product Requirements Document (PRD)**"
    # as "Product Requirements Document (PRD)**" \u2014 close enough to fool the boilerplate check,
    # and close enough to reach the console as a project name with its markdown still attached.
    j = len(s)
    while j > i and is_adornment(s[j - 1]):
        j -= 1
    return s[i:j].strip()


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return (s.replace(" ", " ").replace(" ", " ")
             .replace("‑", "-").replace("–", "-").replace("—", "-")
             .replace("“", '"').replace("”", '"')
             .replace("‘", "'").replace("’", "'"))

#: Prefixes that say what KIND of run this is, not what the app is called. They belong on a
#: badge; leaving them in front made four different apps all display as "QA benchmark".
LABELS = [
    (re.compile(r"^\s*QA benchmark:\s*", re.I), "benchmark"),
    (re.compile(r"^\s*Follow-up:\s*deferred review findings\s*[-:]\s*", re.I), "follow-up"),
    (re.compile(r"^\s*Follow-up:\s*", re.I), "follow-up"),
    (re.compile(r"^\s*Official\s+", re.I), "official"),
    (re.compile(r"^\s*e2e smoke\s*[-:]\s*", re.I), "e2e"),
]


def project_name(text: str) -> tuple[str, str, str]:
    """(name, blurb, label). The name is what a person would call it out loud."""
    t = _norm(text)
    t = re.sub(r"\s*·\s*retry\s*\d+\s*$", "", t, flags=re.I)
    label = ""
    for _ in range(3):  # "Follow-up: … - e2e smoke - …" carries two; strip them all
        for pattern, tag in LABELS:
            if pattern.search(t):
                t = pattern.sub("", t, count=1)
                label = label or tag
                break
        else:
            break
    t = re.sub(r"^\s*(web app|mobile app|service|api)\s*[-—:]\s*", "", t, flags=re.I)
    t = re.sub(r"^\s*(build|create|make)\s+", "", t, flags=re.I)
    t = re.sub(r"^\s*(a|an|the)\s+", "", t, flags=re.I)

    named = ""
    m = re.search(r"\*\*Project:?\*\*\s*(.+)", t) or re.search(r"^\s*Project:\s*(.+)", t, re.M)
    if m:
        named = m.group(1)
    else:
        for line in t.splitlines():
            line = _unadorn(line.strip().lstrip("#"))
            if not line or BOILERPLATE.match(line):
                continue
            line = re.sub(r"\s*[-–—]\s*(product requirements document|prd)\b.*$", "", line, flags=re.I)
            if line and not BOILERPLATE.match(line):
                named = line
                break
    # Boilerplate as a PREFIX ("PRD: BugBox - …"), which the whole-line check cannot see.
    named = re.sub(r"^\s*(product requirements document|prd|design|spec|specification|"
                   r"overview)\s*:\s*", "", named, flags=re.I)
    named = named.strip().rstrip(":—-").strip()
    named = re.sub(r"\((?:working title|draft|v\d[\d.]*)\)", "", named, flags=re.I).strip()

    # A quoted or capitalised short name inside a descriptor is what people actually say.
    q = re.search(r'"([^"]{2,32})"|\'([^\']{2,32})\'', named)
    if q:
        short = (q.group(1) or q.group(2)).strip()
        blurb = re.sub(r'\s*[-–—]?\s*["\']' + re.escape(short) + r'["\']\s*', " ", named).strip(" -–—,")
        return short, blurb[:70], label

    parts = re.split(r"\s+[-–—]\s+|:\s+", named, maxsplit=1)
    name = (parts[0] or named).strip()
    blurb = (parts[1].strip() if len(parts) > 1 else "")
    if len(name) > 42 and blurb == "":
        words = name.split()
        name, blurb = " ".join(words[:5]), " ".join(words[5:])
    # A bare descriptive phrase reads as a sentence fragment on a card. Title-case it so
    # "simple todo list web app" becomes something a person would point at.
    if name and name[:1].islower():
        drop = {"web", "app", "application", "site", "website", "tool"}
        words = [w for w in name.split() if w.lower() not in drop] or name.split()
        name = " ".join(w if w.isupper() else w.capitalize() for w in words[:4])
    # LAST GATE, matching the renderer's. A document TYPE must never reach a caller as a
    # product name — least of all `stable_app_id`, which would hash it into shared identity.
    if BOILERPLATE.match(_unadorn(name)):
        name = ""
    return name[:42] or "Untitled app", blurb[:70], label


def identity_key(name: str) -> str:
    """What makes two names THE SAME APP, for identity and for duplicate refusal.

    `casefold()` alone treated punctuation as meaning. The console showed "Incident Timeline
    Builder." and "Incident-Timeline Builder" as two projects with two PRs and two bills,
    because a trailing full stop and a hyphen produced different hashes — and the duplicate
    guard, which compares the same way, did not refuse the second build either. One product,
    built twice, displayed twice.

    So punctuation and spacing are collapsed: letters and digits in order, single-spaced. The
    DISPLAY name keeps whatever it had; only identity is normalised.
    """
    folded = re.sub(r"[^0-9a-z]+", " ", (name or "").casefold())
    return " ".join(folded.split())


def stable_app_id(project_id: str, repo: str, title: str) -> str:
    """The identity of the APP, decided when the run is created rather than inferred later.

    Five runs of Shorty rendered as four separate cards, three of them dead lineages, while
    the live build sat in a card of its own. The console therefore showed "BLOCKED" for an app
    that was building. `app_id` existed, but it was only ever stamped on the two RETRY paths —
    a fresh `start_code_task` set nothing — so every independently-started run of one app was
    an island, and the console had no fact tying them together.

    Lineage cannot supply this. Lineage answers "what did this run come from", and the runs
    that need grouping most are exactly the ones with no common ancestor: an operator starting
    the app again after a failure creates a root, not a child.

    So identity is derived from what makes two runs the same app — the same project, the same
    repo, the same app name — and is written down at creation. It is NOT title matching in the
    renderer: `project_name` is the same normalisation `active_same_app` already trusts to
    REFUSE to start a duplicate. A definition good enough to block work is good enough to
    group a card.

    Returns "" when the title carries no usable name, so the caller stamps nothing and the run
    keeps lineage-only identity rather than joining a bogus "Untitled app" group.
    """
    import hashlib

    name, _blurb, _label = project_name(title or "")
    if not name or name == "Untitled app":
        return ""
    key = identity_key(name)
    if not key:
        return ""
    material = "\u241f".join([
        str(project_id or "").strip(),
        str(repo or "").strip().casefold(),
        # NOT name.casefold(): punctuation is not identity. See identity_key.
        key,
    ])
    return "app_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
