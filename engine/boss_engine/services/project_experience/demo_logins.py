"""Demo logins for a project (contract §5, §11): WHO you sign in as, per environment, and the
password BOSS generated for it behind an audited, stepped-up reveal.

User names come from the app itself, never invented:
  1. a `demo_logins:` list in the app's boss.deploy.yaml (app_deploy_manifest._demo_logins),
     read from the newest stack deployment's manifest summary or text;
  2. else the users the onboarding agent found in the repository (its seed file or login-page
     hint: `customer@demo.test`), stored on the onboarding record;
     and, failing that, the users the app's last deploy found in the repository at the commit
     it shipped (`discover_in_repository`, recorded on the deploy's manifest summary as
     `demo_logins` with `demo_logins_from: repository`): a project deployed before onboarding
     existed (helpdesk-ai-demo) never had an onboarding record, so Try it, Deployments and
     Settings showed a bare "Demo login" with no name;
  3. else one row with no user name, saying where to declare them.

Passwords are only ever read through templates/secrets.py (`demo_login_values` for the app's
preview/deploy bindings, `deploy_target_demo_logins` for what a deploy target runs with): only
a value BOSS generated, only a demo-login name. `listing` reads names and presence only;
`reveal` is the one function that returns a value, and its route needs owner/admin + step-up.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import as_json, href

DEFAULT_KEY = "DEMO_PASSWORD"
#: An e-mail-shaped demo user in a seed file or login page (`agent@demo.test`).
DEMO_EMAIL = re.compile(
    r"\b([A-Za-z0-9._+-]{1,40}@(?:demo|example|test|seed)\.(?:test|com|local|dev|org|io))\b")
#: Where an app keeps its demo users.
SEED_PATH = re.compile(r"(seed|fixture|demo|login|signin|sign-in|users?)[^/]*\.(?:m?js|ts|tsx|jsx|py|"
                       r"json|ya?ml|sql|rb|go|java|html|md)$", re.I)
MAX_SCAN_FILES = 40
MAX_SCAN_BYTES = 200_000


def _label_for(user: str) -> str:
    head = user.split("@", 1)[0]
    return re.sub(r"[._-]+", " ", head).strip().title() or user


#: WHICH READ NAMED THEM. 2 = only user/login records and a README's sign-in section (an
#: e-mail inside a ticket fixture or sample data is not a user). Deploys and onboarding runs
#: recorded by an older read are read again (backfill_deployed_users) or not trusted.
DISCOVERY_RULE = 2
#: A credential beside the e-mail, in the same record: what makes it a sign-in, not sample text.
_CREDENTIAL = re.compile(r"(pass(?:word|wd|phrase)?|pwd|DEMO_PASSWORD|credentials?|log ?in|sign.?in|role)"
                         r"\s*[\"']?\s*[:=|]", re.I)
#: A file that holds users by its name (seedDemoUsers.mjs, users.json, login.html, auth.ts): each
#: e-mail in it is a user. Any other seed / fixture file (seedDemoTickets.mjs) names a user only
#: where a credential sits in the same record.
_USER_FILE = re.compile(r"(users?|login|signin|sign-in|auth|accounts?|credentials?|people|members?)"
                        r"[^/]*$", re.I)
#: A collection declared as users (`users = [`, `"demoUsers": [`, `accounts: [`): every e-mail in
#: its brackets is a user, whatever file it sits in.
_USER_LIST = re.compile(r"\b\w*(?:users?|accounts?|logins?|credentials?|people|members?)\w*['\"]?\s*[:=]\s*[\[{(]",
                        re.I)
#: A README section that lists who signs in ("## Demo logins", "### Credentials", "Test users").
_SIGN_IN_HEADING = re.compile(r"^\s{0,3}(#{1,6}\s+|\*\*)(.*?(log ?ins?|credentials?|sign.?in|"
                              r"(demo|test|seed(ed)?|sample)\s+(users?|accounts?)|accounts?|users?))\b",
                              re.I)
_HEADING = re.compile(r"^\s{0,3}(#{1,6}\s+|\*\*[^*]+\*\*\s*$)")
#: How far around an e-mail its own record reaches (a JS object, a YAML item, a table row).
_RECORD_REACH = 160


def _record_around(blob: str, start: int, end: int) -> str:
    """The text of the record an e-mail sits in: out to a blank line or `_RECORD_REACH` chars."""
    lo = max(0, start - _RECORD_REACH)
    hi = min(len(blob), end + _RECORD_REACH)
    before = blob[lo:start]
    after = blob[end:hi]
    cut = before.rfind("\n\n")
    before = before[cut + 2:] if cut >= 0 else before
    cut = after.find("\n\n")
    after = after[:cut] if cut >= 0 else after
    # Another record's e-mail bounds this one (a list of users side by side).
    other = DEMO_EMAIL.search(after)
    if other:
        after = after[:other.start()]
    prior = list(DEMO_EMAIL.finditer(before))
    if prior:
        before = before[prior[-1].end():]
    return before + after


def _readme_sections(blob: str) -> list[str]:
    """The parts of a README under a sign-in heading, each to the next heading."""
    out: list[str] = []
    take: list[str] | None = None
    for line in (blob or "").splitlines():
        if _HEADING.match(line):
            if take is not None:
                out.append("\n".join(take))
            take = [] if _SIGN_IN_HEADING.match(line) else None
            continue
        if take is not None:
            take.append(line)
    if take is not None:
        out.append("\n".join(take))
    return out


def _user_list_spans(blob: str) -> list[tuple[int, int]]:
    """Where each collection declared as users begins and ends (bracket matched, bounded)."""
    pairs = {"[": "]", "{": "}", "(": ")"}
    out = []
    for m in _USER_LIST.finditer(blob):
        open_ = blob[m.end() - 1]
        depth, i = 0, m.end() - 1
        limit = min(len(blob), m.end() + 20_000)
        while i < limit:
            ch = blob[i]
            if ch == open_:
                depth += 1
            elif ch == pairs[open_]:
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append((m.end(), i))
    return out


def users_from_files(files: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    """Demo users, in first-seen order (at most 8), from `(path, text)` pairs.

    A USER IS A SIGN-IN, NOT AN E-MAIL. Only two shapes name one:
      * a README's sign-in section ("## Demo logins", "Credentials", "Test users"): its e-mails;
      * a seed / login / fixture file: an e-mail whose own record carries a credential
        (`password:`, `DEMO_PASSWORD`, `role:`…) — a user row, never a ticket's body text; in a file
        named for its users (seedDemoUsers.mjs, users.json, login.html) every e-mail is one.
    `seedDemoTickets.mjs`'s "Seed fixture — safe to ignore. demo@example.test" names nobody.
    """
    seen: dict[str, dict[str, str]] = {}

    def add(user: str) -> bool:
        user = user.lower()
        if user not in seen:
            seen[user] = {"user": user, "label": _label_for(user), "password_key": DEFAULT_KEY}
        return len(seen) >= 8

    for path, blob in files:
        blob = blob or ""
        if path.lower().endswith((".md", ".markdown", ".txt", ".rst")):
            for section in _readme_sections(blob):
                for found in DEMO_EMAIL.findall(section):
                    if add(found):
                        return list(seen.values())
            continue
        named_for_users = bool(_USER_FILE.search(path.rsplit("/", 1)[-1])) if path else False
        lists = _user_list_spans(blob)
        for m in DEMO_EMAIL.finditer(blob):
            if named_for_users or any(a <= m.start() < b for a, b in lists) \
                    or _CREDENTIAL.search(_record_around(blob, m.start(), m.end())):
                if add(m.group(1)):
                    return list(seen.values())
    return list(seen.values())


def users_from_text(blobs: Iterable[str]) -> list[dict[str, str]]:
    """Kept for callers holding text without a path: read as code (credential beside it)."""
    return users_from_files(("", b) for b in blobs)


def scan_checkout(root: Any, paths: list[str]) -> list[dict[str, str]]:
    """The onboarding agent's read: seed / login files of a checkout (bounded, data only)."""
    from pathlib import Path

    base = Path(root)
    wanted = [p for p in paths if SEED_PATH.search(p) and "node_modules" not in p][:MAX_SCAN_FILES]
    wanted += [p for p in ("README.md", "readme.md") if p in paths]
    files = []
    for rel in wanted:
        try:
            with open(base / rel, "rb") as fh:
                files.append((rel, fh.read(MAX_SCAN_BYTES).decode("utf-8", "replace")))
        except OSError:
            continue
    return users_from_files(files)


def seed_paths(connection: Connection, project_id: str, repository: str) -> list[str]:
    """Where this repository keeps its demo users: the code graph's files that look like a
    seed / login / fixture file (SEED_PATH), plus the README. Paths only; bounded."""
    rows = connection.execute(text(
        "SELECT DISTINCT path FROM kg_nodes WHERE project_id = :p AND kind = 'file' "
        "AND (repo IS NULL OR lower(repo) = lower(:r)) AND path IS NOT NULL LIMIT 5000"),
        {"p": project_id, "r": repository}).scalars().all()
    paths = sorted(str(p) for p in rows if SEED_PATH.search(str(p)) and "node_modules" not in str(p)
                   and "/test" not in f"/{p}" and not str(p).startswith("tests/"))
    return paths[:MAX_SCAN_FILES // 4] + ["README.md"]


def discover_in_repository(connection: Connection, project_id: str, repository: str,
                           read: Any) -> list[dict[str, str]]:
    """The demo users an app names in its own files at the commit being deployed: `read(path)`
    returns a file's text or None (the deploy's GitHub reader). Names only; never raises."""
    files = []
    for path in seed_paths(connection, project_id, repository):
        try:
            got = read(path)
        except Exception:  # noqa: BLE001 — a missing or unreadable file names nobody
            got = None
        if got:
            files.append((path, str(got)[:MAX_SCAN_BYTES]))
    return users_from_files(files)


def backfill_deployed_users(engine: Any, settings: Any, *, read_for: Any = None,
                            limit: int = 20, project_id: Optional[str] = None) -> list[str]:
    """THE ONE-TIME REPAIR for apps deployed before deploys read their demo users: the newest
    healthy deploy of each project whose manifest names none (and generated a demo password)
    reads the app's seed/login files at the commit it shipped and records the names on that
    deploy's manifest summary (demo_logins_from: repository). Idempotent (a deploy that has
    names is skipped); `read_for(connection, project_id, owner, repo)` → read(path, commit)
    defaults to the deploy's own GitHub reader. Returns the deploy ids it named."""
    import json

    from sqlalchemy import text as _text

    with engine.connect() as c:
        rows = c.execute(_text(
            "SELECT DISTINCT ON (s.target_id) s.id, s.project_id, s.repository, s.commit_sha, "
            "s.manifest FROM stack_deployments s WHERE s.status = 'healthy' "
            "AND (CAST(:p AS TEXT) IS NULL OR s.project_id = :p) "
            "ORDER BY s.target_id, s.created_at DESC"), {"p": project_id}).mappings().all()
    def _due(m: dict) -> bool:
        # Never named, or named from the repository by an older read (which took any e-mail in
        # a fixture): read again with today's rule.
        if not m.get("generated_env_keys"):
            return False
        if not m.get("demo_logins"):
            return int(m.get("demo_logins_rule") or 0) < DISCOVERY_RULE
        return (m.get("demo_logins_from") == "repository"
                and int(m.get("demo_logins_rule") or 0) < DISCOVERY_RULE)

    todo = [dict(r) for r in rows if _due(as_json(r["manifest"], {}) or {})
            and "/" in str(r["repository"] or "")][:limit]
    named: list[str] = []
    for r in todo:
        owner, repo = str(r["repository"]).split("/", 1)
        try:
            with engine.connect() as c:
                if read_for is None:
                    from ..stack_deploy import github_source_for

                    source = github_source_for(c, settings, r["project_id"], owner, repo)
                    read = lambda path, _s=source: _s.read_file(owner, repo, path, r["commit_sha"])
                else:
                    read = read_for(c, r["project_id"], owner, repo)
                found = discover_in_repository(c, r["project_id"], str(r["repository"]), read)
        except Exception:  # noqa: BLE001 — a repo we cannot read names nobody
            continue
        had = bool((as_json(r["manifest"], {}) or {}).get("demo_logins"))
        if not found and not had:
            # Nobody to name; remember the read so it is not repeated at every boot.
            with engine.begin() as c:
                c.execute(_text(
                    "UPDATE stack_deployments SET manifest = COALESCE(manifest, '{}'::jsonb) || "
                    "CAST(:m AS JSONB) WHERE id = :i"),
                    {"i": r["id"], "m": json.dumps({"demo_logins_rule": DISCOVERY_RULE})})
            continue
        with engine.begin() as c:
            c.execute(_text(
                "UPDATE stack_deployments SET manifest = COALESCE(manifest, '{}'::jsonb) || "
                "CAST(:m AS JSONB) WHERE id = :i"),
                {"i": r["id"], "m": json.dumps({"demo_logins": found,
                                                "demo_logins_from": "repository",
                                                "demo_logins_rule": DISCOVERY_RULE})})
        named.append(str(r["id"]))
    return named


def _manifest_users(connection: Connection, project_id: str) -> list[dict[str, str]]:
    from ..app_deploy_manifest import ManifestError, load_manifest

    rows = connection.execute(text(
        "SELECT s.manifest, s.manifest_text FROM stack_deployments s JOIN deployment_targets t "
        "ON t.id = s.target_id WHERE t.project_id = :p ORDER BY s.created_at DESC LIMIT 3"),
        {"p": project_id}).mappings().all()
    for r in rows:
        summary = as_json(r["manifest"], {}) or {}
        listed = summary.get("demo_logins")
        if summary.get("demo_logins_from") == "repository":
            listed = None
        if isinstance(listed, list) and listed:
            return [_person(d) for d in listed if isinstance(d, dict) and d.get("user")]
        if r["manifest_text"] and "demo_logins" in str(r["manifest_text"]):
            try:
                parsed = load_manifest(str(r["manifest_text"]))
            except (ManifestError, Exception):  # noqa: BLE001 — a bad manifest names no users
                continue
            if parsed.demo_logins:
                return [_person(d) for d in parsed.demo_logins]
    return []


def _person(d: dict) -> dict[str, str]:
    """A demo user by name: {user, label, password_key} (the env key that holds the password;
    the manifest calls it `password`). Never a value."""
    return {"user": str(d.get("user")), "label": str(d.get("label") or d.get("user")),
            "password_key": str(d.get("password_key") or d.get("password") or DEFAULT_KEY)}


def _discovered_users(connection: Connection, project_id: str) -> list[dict[str, str]]:
    row = connection.execute(text(
        "SELECT facts FROM project_onboarding_runs WHERE project_id = :p "
        "AND facts ? 'demo_users' ORDER BY started_at DESC LIMIT 1"),
        {"p": project_id}).mappings().first()
    facts = (as_json(row["facts"], {}) or {}) if row else {}
    if int(facts.get("demo_users_rule") or 0) < DISCOVERY_RULE:
        return []   # read by an older rule; the next onboarding pass reads it again
    users = facts.get("demo_users")
    return [_person(u) for u in users or [] if isinstance(u, dict) and u.get("user")]


def _deployed_users(connection: Connection, project_id: str) -> list[dict[str, str]]:
    """The users the app's newest deploy found in its repository (stack_deploy records them on
    the manifest summary when boss.deploy.yaml names none)."""
    rows = connection.execute(text(
        "SELECT s.manifest FROM stack_deployments s JOIN deployment_targets t ON t.id = s.target_id "
        "WHERE t.project_id = :p AND s.manifest->>'demo_logins_from' = 'repository' "
        "ORDER BY s.created_at DESC LIMIT 1"), {"p": project_id}).scalars().all()
    for manifest in rows:
        summary = as_json(manifest, {}) or {}
        if int(summary.get("demo_logins_rule") or 0) < DISCOVERY_RULE:
            return []   # an older read took e-mails out of fixtures: the backfill reads it again
        listed = summary.get("demo_logins") or []
        return [_person(d) for d in listed if isinstance(d, dict) and d.get("user")]
    return []


def users(connection: Connection, project_id: str) -> tuple[list[dict[str, str]], str]:
    """(users, where they came from: manifest | repository | none)."""
    found = _manifest_users(connection, project_id)
    if found:
        return found, "manifest"
    # What the newest deploy read at the commit it shipped is more current than onboarding's read.
    found = _deployed_users(connection, project_id) or _discovered_users(connection, project_id)
    if found:
        return found, "repository"
    return [], "none"


def _env_label(env: str) -> str:
    from .health import env_label

    return env_label(env)


def environments(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Each environment that has a BOSS-generated demo password, by NAME only (no value):
    preview (the app's preview binding) and each deploy target whose healthy deploy generated
    one (or, failing that, the app's deploy binding)."""
    from ...templates.run_env import is_demo_login_name

    out: dict[str, dict[str, Any]] = {}
    rows = connection.execute(text(
        "SELECT DISTINCT env, name FROM app_secret_bindings WHERE project_id = :p "
        "AND mode = 'generated' AND vault_item_id IS NOT NULL"), {"p": project_id}).mappings().all()
    bound = {(r["env"], r["name"]) for r in rows if is_demo_login_name(str(r["name"]))}
    if any(e == "preview" for e, _ in bound):
        out["preview"] = {"key": "preview", "label": "Preview",
                          "keys": sorted(n for e, n in bound if e == "preview")}
    targets = connection.execute(text(
        "SELECT t.id, t.environment, s.manifest FROM deployment_targets t "
        "LEFT JOIN LATERAL (SELECT manifest FROM stack_deployments WHERE target_id = t.id "
        " AND status = 'healthy' ORDER BY created_at DESC LIMIT 1) s ON TRUE "
        "WHERE t.project_id = :p AND t.status = 'active' ORDER BY t.environment"),
        {"p": project_id}).mappings().all()
    deploy_keys = sorted(n for e, n in bound if e == "deploy")
    for t in targets:
        manifest = as_json(t["manifest"], {}) or {}
        keys = sorted(str(k) for k in manifest.get("generated_env_keys") or []
                      if is_demo_login_name(str(k))) or deploy_keys
        if not keys:
            continue
        env = str(t["environment"] or "deploy")
        slot = out.setdefault(env, {"key": env, "label": _env_label(env), "keys": []})
        slot["keys"] = sorted(set(slot["keys"]) | set(keys))
        slot.setdefault("target_ids", []).append(t["id"])
    return list(out.values())


def reveal_href(project_id: str, env: str) -> str:
    return f"/v1/projects/{project_id}/demo-logins/reveal?env={env}"


def rows_for(project_id: str, env: dict[str, Any], people: list[dict[str, str]]
             ) -> list[dict[str, Any]]:
    """The read-only rows (user + reveal) for one environment."""
    reveal = {"href": reveal_href(project_id, env["key"]), "requires": "step_up",
              "method": "GET", "roles": ["owner", "admin"]}
    if not people:
        return [{"user": None, "label": "Demo login", "env": env["key"],
                 "password_key": env["keys"][0] if env.get("keys") else DEFAULT_KEY,
                 "reveal": reveal,
                 "hint": "Name the demo users in boss.deploy.yaml (demo_logins:) to show them here."}]
    keys = set(env.get("keys") or [])
    return [{"user": p["user"], "label": p.get("label") or p["user"], "env": env["key"],
             "password_key": p.get("password_key") or DEFAULT_KEY, "reveal": reveal}
            for p in people if not keys or (p.get("password_key") or DEFAULT_KEY) in keys
            or len(keys) == 1]


def listing(connection: Connection, project_id: str) -> dict[str, Any]:
    """Names only, every environment: what Overview › Try it, Deployments and Settings show."""
    people, source = users(connection, project_id)
    envs = environments(connection, project_id)
    return {"source": source, "users": people,
            "environments": [{**e, "logins": rows_for(project_id, e, people)} for e in envs],
            "settings_href": href(project_id, area="project", tab="settings",
                                  drawer="section:demo_logins")}


def _app_keys(connection: Connection, project_id: str) -> list[str]:
    """The apps of this project that hold a generated secret binding."""
    return [str(r) for r in connection.execute(text(
        "SELECT DISTINCT app_key FROM app_secret_bindings WHERE project_id = :p AND "
        "mode = 'generated' ORDER BY app_key"), {"p": project_id}).scalars().all()]


def reveal(connection: Connection, settings: Any, *, org_id: str, project_id: str,
           env: str) -> dict[str, Any]:
    """The ONE place this module returns a password: {env, logins: [{user, label, password}]}.
    Only values BOSS generated, read through templates/secrets.py: a deploy target's generated
    secret (`target_demo_logins`), else the app's binding for that environment
    (`demo_login_values`, preview / deploy)."""
    from ...templates import secrets as app_secrets

    env = str(env or "").strip().lower()
    known = {e["key"]: e for e in environments(connection, project_id)}
    if env not in known:
        raise LookupError(f"No demo login was generated for {env or 'that environment'}.")
    slot = known[env]
    values: dict[str, str] = {}
    if env != "preview":
        for t in connection.execute(text(
                "SELECT t.id, t.environment, s.manifest FROM deployment_targets t "
                "JOIN LATERAL (SELECT manifest FROM stack_deployments WHERE target_id = t.id "
                " AND status = 'healthy' ORDER BY created_at DESC LIMIT 1) s ON TRUE "
                "WHERE t.id = ANY(:ids) AND t.project_id = :p"),
                {"ids": slot.get("target_ids") or [], "p": project_id}).mappings().all():
            for row in app_secrets.target_demo_logins(connection, settings, project_id=project_id,
                                                      target=dict(t)):
                values.setdefault(str(row["name"]), str(row["value"]))
    if not values:
        for app_key in _app_keys(connection, project_id):
            got = app_secrets.demo_login_values(
                connection, org_id=org_id, project_id=project_id, app_key=app_key,
                env="preview" if env == "preview" else "deploy")
            for name, value in got.items():
                values.setdefault(name, value)
    people, _source = users(connection, project_id)
    logins = []
    for row in rows_for(project_id, slot, people):
        key = row["password_key"]
        value = values.get(key) or (next(iter(values.values())) if len(values) == 1 else None)
        logins.append({"user": row["user"], "label": row["label"], "password_key": key,
                       "password": value})
    return {"project_id": project_id, "env": env, "label": slot["label"], "logins": logins}


def record_reveal(connection: Connection, project_id: str, actor: str, env: str, count: int) -> None:
    """An `events` row: who revealed which environment's demo logins (never the value)."""
    from ..ambient._common import emit

    emit(connection, project_id, "project.demo_logins_revealed", actor, "project", project_id,
         {"env": env, "logins": count})


__all__ = ["DEFAULT_KEY", "DISCOVERY_RULE", "users_from_files", "backfill_deployed_users", "discover_in_repository", "environments", "listing", "record_reveal", "reveal", "reveal_href",
           "rows_for", "scan_checkout", "seed_paths", "users", "users_from_text"]
