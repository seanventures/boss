"""THE run-time environment an app needs, from what the app DECLARES. One answer for every runner.

WHY. HelpDesk AI (crun_db538dff872549bf9359) finished its writing and review in 19 minutes and was
then blocked by the app-start proof: "SESSION_SECRET must be set to at least 32 characters (BOSS
generates one)". The app was right to refuse. BOSS generated SESSION_SECRET for a DEPLOY
(secrets.ensure_deploy_generated) and stored one for the preview, but the serve proof started the
app with nothing: three runners, three ideas of what the app is given. Now the deploy, the
app-start proof, the preview and the connector smoke check (which probes the proof's app) all
decide each declared variable with `decide` below.

WHAT AN APP DECLARES (names only; never a value from a file):
  * its templates' and connectors' secret specs (`SecretSpec`: test_default, required);
  * its boss.deploy.yaml `env:` entries — `{generate: ...}` BOSS mints, `{secret: ...}` a person sets
    FOR A DEPLOY (deploy_only: never a blocker of a throwaway run — the proof and preview get the
    run's own database instead of the deploy's DATABASE_URL);
  * a credential .env.example MARKS REQUIRED (`# Required secret: …` above it, or `# required`
    on its line) is a secret only a person has (ENT-REC-13): the app is not started, a person is
    asked (`is_person_only_name`). An existing app's own files are the only place it can say so;
    without this the coder was told the app "must start without" its payments key.
  * a credential the app's START ERROR names as missing ("PAYMENTS_API_KEY is not set") and no
    file declares at all is one too (`RunEnvironment.missing_secret_named_in`): a person's card,
    never a coder told to fake or stub the credential. A non-credential name (PAGE_SIZE) stays
    the coder's: BOSS can supply a default.
  * its .env.example names. Only a session/signing-key name there is BOSS's to mint (SESSION_SECRET,
    JWT_SECRET, SECRET_KEY, ...); every other name is "listed": OPTIONAL. It never blocks, and an
    app that dies naming it is the CODER's to fix (it must start without it, that feature off) —
    never a person's "needs a secret" (crun_0d82ab62: HelpDesk AI refused to start without the
    listed DEMO_PASSWORD and was blocked for a person, with no repair round, after 19.4 min).
  * a DEMO/SEED LOGIN PASSWORD named there (DEMO_PASSWORD, ADMIN_PASSWORD, SEED_ADMIN_PASSWORD…,
    `_DEMO_LOGIN_NAME`) is "demo": BOSS mints a fresh one for every throwaway run, as it does a
    session key — a brief that says "the seed refuses to run without DEMO_PASSWORD" is right — and
    a preview SHOWS it to its reviewer (`RunEnvironment.shown`), or nobody could sign in. A deploy
    uses the value a person set, else leaves it unset.

WHAT BOSS SUPPLIES ITSELF wins over "listed"/"unset": the launcher's PORT and HOST, the run's
build-services database (DATABASE_URL, PG*), and any value BOSS issues for this launch
(`RunEnvironment.supply`). A declared name is "unset" only when nothing supplies it.

THE RULES (`decide`):
  * generated — a throwaway run (proof, preview, smoke) gets a FRESH random value every time; it is
    never stored and never reused, so a deploy can never inherit it. A deploy uses its own stored
    deploy value (ensure_deploy_generated makes it once).
  * stub / optional — left unset when nothing is bound, so the app runs without that provider:
    exactly what a deploy does with a `required: false` secret.
  * a required secret a person must set — its bound value when there is one; otherwise the run is
    not started and the verdict names the variable ("needs a secret"), never a crash. ONLY this
    kind (`person_must_supply`) is a person's act; a crash naming anything else is the code's.
"""
from __future__ import annotations

import re
import secrets as _random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

#: Runs whose generated values are throwaway: minted fresh, never stored, never a deploy's.
THROWAWAY = ("proof", "preview", "smoke")
PURPOSES = THROWAWAY + ("deploy",)

#: A name in .env.example that is a key BOSS owns: it signs the app's own sessions or tokens and no
#: person is meant to choose it. A provider's CLIENT_SECRET or API key never matches.
_BOSS_OWNED_NAME = re.compile(
    r"^(?:[A-Z0-9]+_)?(?:SESSION|COOKIE|JWT|CSRF|NEXTAUTH|AUTH|SIGNING|ENCRYPTION)_(?:SECRET|KEY)$"
    r"|^(?:APP_)?SECRET_KEY(?:_BASE)?$"
    # A web framework's own signing key (Flask, Django, Rails…) and an app's own secret or
    # incoming-webhook signing secret: the app's to sign with, never a provider's.
    r"|^(?:FLASK|DJANGO|RAILS|LARAVEL|RACK|EXPRESS|PHOENIX|APP)_SECRET(?:_KEY(?:_BASE)?)?$"
    r"|^WEBHOOK_(?:SIGNING_)?SECRET$")

#: Names that are never a person's to supply, whatever their shape: keys the app signs with that
#: are not plain strings (a JWT private key, any *_SIGNING_* material), the app's own internal or
#: admin tokens — the coder's (or BOSS's) to provide for a run, never a person's card.
_APP_OWN_CREDENTIAL = re.compile(
    r"(?:^|_)SIGNING(?:_|$)|(?:^|_)JWT_(?:PRIVATE|PUBLIC)_KEY$"
    r"|(?:^|_)(?:ADMIN|INTERNAL|SERVICE|MASTER|ROOT|LOCAL|DEV|TEST|SYSTEM)_(?:[A-Z0-9]+_)?"
    r"(?:TOKEN|SECRET|KEY|API_KEY)$")

#: Credentials a person must NEVER be asked to hand to code a build wrote: BOSS's own, a code
#: host's and a cloud account's (agent_env.NEVER names the rest). They stay out of every app.
_NEVER_A_PERSONS_PREFIXES = (
    "BOSS_", "GITHUB_", "GH_", "GITLAB_", "BITBUCKET_", "AWS_", "GCP_", "GCLOUD_",
    "GOOGLE_APPLICATION_", "GOOGLE_CLOUD_", "AZURE_", "ARM_", "S3_", "MINIO_")

#: A credential a PROVIDER issues (an external service's API key, an OAuth client secret, a
#: bot/auth/access token): the only shape the start-error path asks a person for.
_PROVIDER_CREDENTIAL = re.compile(
    r"^[A-Z0-9]+(?:_[A-Z0-9]+)*_(?:API_KEY|APIKEY|API_SECRET|API_TOKEN|CLIENT_SECRET|BOT_TOKEN"
    r"|AUTH_TOKEN|ACCESS_TOKEN|SECRET_KEY|PRIVATE_API_KEY)$")

#: A demo/seed login's password: a value the app's seed script signs its demo users in with. Not a
#: provider's credential and not a database's (POSTGRES_PASSWORD, PGPASSWORD, SMTP_PASSWORD…).
_DEMO_LOGIN_NAME = re.compile(
    r"^(?:[A-Z0-9]+_)*(?:DEMO|SEED|ADMIN|DEFAULT_ADMIN|INITIAL_ADMIN|TEST_USER)(?:_[A-Z0-9]+)*_PASSWORD$")
_NOT_A_DEMO_LOGIN = re.compile(r"(?:^|_)(?:DB|DATABASE|POSTGRES|PG|MYSQL|REDIS|SMTP|MAIL|SMTPS)(?:_|$)")

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

#: A name BOSS's own build services supply to a throwaway run (the run's database), never a
#: person's to set for one.
_BOSS_SERVICE_NAME = re.compile(r"(?:^|_)(?:DB|DATABASE|POSTGRES|PG|MYSQL|REDIS|MONGO)(?:_|$)")

#: A comment that marks the variable under (or beside) it as required — and the words that say it
#: is not, or not for a build: "isn't required", "no longer required", "required only in
#: production", "optional". "Required secret: the payments key" / "# required" are marks.
_REQUIRED_MARK = re.compile(r"\brequired\b", re.IGNORECASE)
_NOT_REQUIRED_MARK = re.compile(
    r"\boptional\b|\b(?:not|isn'?t|aren'?t|no\s+longer|never)\s+(?:\w+\s+)?required\b"
    r"|\bnon-required\b|\brequired\s+(?:only\s+)?(?:in|for|on)\s+(?:prod|production|deploy|live|"
    r"staging)\b|\bonly\s+(?:in|for|on)\s+(?:prod|production|deploy|live|staging)\b",
    re.IGNORECASE)

#: How a start error says ONE variable is missing — the name and the words about it together:
#: "X is not set", "X must be set", "X missing", "missing X", "Missing required environment
#: variable: X", "please set X". Applied to the line the app died on, never to a warning above.
_SAID_MISSING_AFTER = (r"\W{{0,3}}(?:(?:is|are|was)\s+)?(?:not\s+set|missing|required|undefined"
                       r"|empty|not\s+configured|not\s+provided|must\s+be\s+(?:set|provided"
                       r"|defined|configured))")
_SAID_MISSING_BEFORE = (r"(?:missing|required|set|provide|define|configure)\b[\w\s:`'\"-]{{0,40}}?")
_ENV_NAME = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")

#: How long a generated throwaway value is: token_urlsafe(32) is 43 characters, past every
#: "at least 32 characters" check an app makes.
GENERATED_BYTES = 32

#: Set by the launcher on every start (preview_service.app_environment), whatever is declared.
PLATFORM_NAMES = ("PORT", "HOST")

#: Told to the coder up front (implement checklist, sandbox facts), so the first pass is right.
OPTIONAL_SETTINGS_RULE = (
    "Optional settings (anything only listed in .env.example: provider client ids, base URLs, "
    "feature flags) must never stop the app from starting; turn the feature off and log it. A "
    "demo/seed login password (DEMO_PASSWORD, ADMIN_PASSWORD, SEED_*_PASSWORD) is generated by "
    "BOSS for every app-start check and preview (and shown to the reviewer): read it from the "
    "environment, never hard-code one.")


def is_demo_login_name(name: str) -> bool:
    return bool(_DEMO_LOGIN_NAME.match(name or "")) and not _NOT_A_DEMO_LOGIN.search(name or "")


def is_person_only_name(name: str) -> bool:
    """Is `name` a credential only a person has (a provider's API key, a client secret, a
    token)? Credential-shaped (agent_env.SECRET_SHAPED, the shape BOSS withholds from every app
    it starts), and none of what BOSS or the coder supplies: a session/signing key BOSS mints,
    a key the app signs with, its own internal/admin token, a demo login's password, the run's
    database, PORT/HOST. And never a credential no person may hand to code a build wrote:
    agent_env.NEVER, BOSS's own (BOSS_*), a code host's or a cloud account's."""
    from ..services.agent_env import NEVER, SECRET_SHAPED

    upper = str(name or "").upper()
    if not upper or upper in PLATFORM_NAMES or upper in NEVER or _BOSS_OWNED_NAME.match(upper):
        return False
    if upper.startswith(_NEVER_A_PERSONS_PREFIXES) or _APP_OWN_CREDENTIAL.search(upper):
        return False
    if is_demo_login_name(upper) or _BOSS_SERVICE_NAME.search(upper):
        return False
    return any(marker in upper for marker in SECRET_SHAPED)


def is_provider_credential(name: str) -> bool:
    """A person-only name (is_person_only_name) shaped like what an external provider issues
    (STRIPE_SECRET_KEY, PAYMENTS_API_KEY, GITHUB_CLIENT_SECRET… minus the excluded hosts,
    SLACK_BOT_TOKEN, TWILIO_AUTH_TOKEN). The bar for asking a person from a start error alone."""
    upper = str(name or "").upper()
    return is_person_only_name(upper) and bool(_PROVIDER_CREDENTIAL.match(upper))


def said_missing(line: str, name: str) -> bool:
    """Does `line` say that `name` itself is missing (not merely mention it)?"""
    n = re.escape(name)
    return bool(re.search(rf"\b{n}\b" + _SAID_MISSING_AFTER.format(), line, re.IGNORECASE)
                or re.search(_SAID_MISSING_BEFORE.format() + rf"\b{n}\b", line, re.IGNORECASE))


def death_line(log_text: str) -> Optional[str]:
    """The line the app died on: the LAST line of its output that reads as an error
    (_ERROR_LINE), stack frames skipped. A warning it logged and survived is not it."""
    for line in reversed((log_text or "").splitlines()):
        bare = line.strip()
        if not bare or bare.startswith(("at ", 'File "')) or re.match(r"^\S+\.(?:go|py|js|ts):\d+", bare):
            continue
        if _ERROR_LINE.search(line):
            return line
    return None


#: The platform's demo-seed opt-in: the variable an app's seed script gates itself on (the
#: coder checklist and the deploy misfit hint both name it). An app that declares a demo login
#: is declaring demo seeding; this flag is how a run turns it on.
DEMO_SEED_FLAG = "ALLOW_DEMO_SEED"

#: Target environments where demo users belong: rehearsals. Anything NOT clearly a rehearsal —
#: "production", "live", an operator's own name for a customer-facing environment, or no name
#: at all (create_stack_target defaults to production) — is production-kind and never seeded.
_DEMO_SEED_ENVIRONMENTS = frozenset((
    "staging", "stage", "preview", "previews", "demo", "dev", "development",
    "test", "testing", "qa", "uat", "sandbox", "integration"))


def demo_seed_target(environment: Optional[str]) -> bool:
    """Is this deploy environment one whose runs get demo users seeded? Previews and
    staging-kind targets only; an unknown or empty name counts as production."""
    return str(environment or "").strip().lower() in _DEMO_SEED_ENVIRONMENTS


def declares_demo_seeding(declared: Mapping[str, "Need"]) -> bool:
    """The app's DECLARED contract for demo seeding, never a match on the app's name: it names
    the opt-in flag itself (boss.deploy.yaml / .env.example), or declares a demo login password
    (kind "demo") whose seeded users are what the flag turns on."""
    return DEMO_SEED_FLAG in (declared or {}) or any(
        n.kind == "demo" for n in (declared or {}).values())


def with_demo_seed(values: Mapping[str, str], *, environment: Optional[str],
                   person_set: Iterable[str] = ()) -> dict[str, str]:
    """A DEPLOY's environment with the demo-seed policy applied (a throwaway run gets it in
    `resolve`). A staging-kind target whose environment carries a demo login password gets
    ALLOW_DEMO_SEED=true, so the seeded users the password belongs to actually exist
    (HelpDesk's staging revealed a DEMO_PASSWORD that opened nothing: the seed never ran). A
    production-kind target NEVER carries it — not even as a literal the repository's manifest
    wrote, because the repo does not get to seed demo users into production — unless a person
    deliberately bound it for this app's deploy (`person_set` names those keys)."""
    out = dict(values or {})
    if not demo_seed_target(environment):
        if DEMO_SEED_FLAG not in set(person_set or ()):
            out.pop(DEMO_SEED_FLAG, None)
        return out
    if DEMO_SEED_FLAG not in out and any(is_demo_login_name(k) for k in out):
        out[DEMO_SEED_FLAG] = "true"
    return out


@dataclass(frozen=True)
class Need:
    """One variable the app declares. `kind`: generated | demo | secret | stub | listed | literal.
    `deploy_only`: a person sets it for a DEPLOY (boss.deploy.yaml `{secret}`); a throwaway run
    never waits for it. `value`: a literal's setting (boss.deploy.yaml `KEY: "true"`), never a
    secret."""
    name: str
    kind: str
    required: bool
    source: str
    deploy_only: bool = False
    value: Optional[str] = None

    def where(self) -> str:
        return f"declared by {self.source}"

    def person_must_supply(self) -> bool:
        """Only a person can supply it: a required secret with no test default (a template's or
        connector's SecretSpec, or boss.deploy.yaml `{secret}`). Listed, optional, stub and
        generated names never are."""
        return self.kind == "secret" and self.required and not self.deploy_only


@dataclass
class RunEnvironment:
    """What one run of the app is given. `values` is never logged or returned to a caller outside
    the engine; everything else is names only."""
    purpose: str
    values: dict[str, str] = field(default_factory=dict, repr=False)
    generated: list[str] = field(default_factory=list)
    unset: list[str] = field(default_factory=list)
    needs: list[Need] = field(default_factory=list)
    declared: dict[str, Need] = field(default_factory=dict)
    supplied: list[str] = field(default_factory=list)
    #: Generated values a reviewer must be shown (a demo login's password), names only.
    shown: list[str] = field(default_factory=list)
    #: How a card names the app ("helpdesk", or "the app" at the repository root).
    app_label: str = "the app"

    def shown_values(self) -> dict[str, str]:
        """{name: value} of the demo logins this run generated, for the preview's reviewer."""
        return {n: self.values[n] for n in self.shown if n in self.values}

    def supply(self, values: Optional[Mapping[str, str]] = None, *,
               names: Iterable[str] = ()) -> "RunEnvironment":
        """Values BOSS itself gives this launch (build services' DATABASE_URL/PG*, a preview's
        issued variables) and `names` the launcher sets (PORT, HOST). They WIN: a supplied name
        is never "unset" and never "needs a secret". Returns self."""
        given = {k: v for k, v in (values or {}).items() if isinstance(v, str) and v}
        for name in names:
            # THE LAUNCHER'S NAME CARRIES THE LAUNCHER'S VALUE. A value declared or bound for it
            # (boss.deploy.yaml `PORT: "3000"`, a literal every run gets since 1aee54c41) rode
            # in on `values`, which launch_app applies AFTER its own PORT: the app bound 3000
            # while BOSS waited on the port it had chosen, and a known-good app "did not answer
            # HTTP within 90s" (the release gate, 2026-11-07).
            self.values.pop(name, None)
        for name in [*given, *names]:
            if name in self.unset:
                self.unset.remove(name)
            self.needs = [n for n in self.needs if n.name != name]
            if name in given and name in self.generated:
                self.generated.remove(name)
            if name in given and name in self.shown:
                self.shown.remove(name)
            if name not in self.supplied:
                self.supplied.append(name)
        self.values.update(given)
        return self

    def verdict(self) -> Optional[str]:
        """"needs a secret: STRIPE_SECRET_KEY (declared by template payments)" or None."""
        if not self.needs:
            return None
        named = ", ".join(f"{n.name} ({n.where()})" for n in self.needs)
        return f"needs a secret: {named}. BOSS cannot make it; set it in the app's Secrets"

    def evidence(self) -> dict:
        """Names only, for a record or an event."""
        return {"generated": list(self.generated), "unset": list(self.unset),
                "supplied": list(self.supplied), "shown": list(self.shown),
                "declared": sorted(self.declared),
                "needs_secret": [n.name for n in self.needs]}

    def unset_named_in(self, log_text: str) -> Optional[str]:
        """The declared variable this run was not given that the app's log names on an ERROR
        line ("X must be set", "missing X", "Error: …X…"), the LAST such line winning: the line
        the app died on. A variable merely mentioned (a startup banner, a connection URL echoed in
        a refusal the app logged and survived) is not why it died."""
        if not self.unset:
            return None
        for line in reversed((log_text or "").splitlines()):
            if not _ERROR_LINE.search(line):
                continue
            for name in self.unset:
                if re.search(rf"\b{re.escape(name)}\b", line):
                    return name
        return None

    def missing_secret_named_in(self, log_text: str) -> Optional[str]:
        """A provider's credential (is_provider_credential) that the line the app DIED on
        (death_line) says is missing, that nothing declares and this run was not given — the
        app's own config read, seen at the moment it refuses to start. Only that line: a warning
        above it ("STRIPE_SECRET_KEY missing, payments disabled") followed by an unrelated
        TypeError is the coder's crash, never a person's card. A declared name is
        unset_named_in's (its declaration decides whose it is)."""
        line = death_line(log_text)
        if not line:
            return None
        given = set(self.values.values())
        for m in _ENV_NAME.finditer(line):
            name = m.group(0)
            if (name in self.declared or name in self.values or name in self.supplied
                    or name in given):
                continue
            if is_provider_credential(name) and said_missing(line, name):
                return name
        return None

    def declare_named(self, name: str, source: str) -> Need:
        """Record a name the app's start error asked for as DECLARED (with where it came from),
        so the run's evidence carries it and the app's Secrets accept a value for every env a
        run reaches (secrets.app_declared_env → deploy_value_spec), as the card asks."""
        need = Need(name, "secret", True, source)
        self.declared[name] = need
        if all(n.name != name for n in self.needs):
            self.needs.append(need)
        return need

    def crash_needs_person(self, log_text: str) -> Optional[str]:
        """The variable the crash names when only a person can supply it (person_must_supply),
        else None — a crash on a listed or optional variable is the code's to fix."""
        name = self.unset_named_in(log_text)
        need = self.declared.get(name) if name else None
        if need is not None and need.person_must_supply():
            return name
        return None if name else self.missing_secret_named_in(log_text)

    def explain_crash(self, log_text: str) -> Optional[str]:
        """When the app died naming a declared variable it was not given, say THAT, by name:
        "needs a secret" only for one a person must supply; otherwise that the app must start
        without it."""
        name = self.unset_named_in(log_text)
        if name is None:
            missing = self.missing_secret_named_in(log_text)
            if missing:
                return (f"needs a secret: {missing} (named by {self.app_label}'s start error). "
                        "BOSS cannot make it and did not give it to this run; set it in the "
                        "app's Secrets")
            return None
        need = self.declared.get(name)
        if need is not None and need.person_must_supply():
            return (f"needs a secret: {name} ({need.where()}). BOSS did not give it to this "
                    "run; set it in the app's Secrets")
        if need is None:
            why = "it is not something BOSS or a person is asked to set"
        elif need.kind == "listed":
            why = f"it is optional (listed in {need.source})"
        else:
            why = f"it is optional for this run ({need.where()})"
        return (f"must start without {name}: {why}. Turn off what needs it when it is unset "
                "(hide that sign-in, skip that integration) and log that it is off")


#: Every environment a run of the app reaches; the Secrets API accepts each for a name the app
#: declares (secrets.deploy_value_spec), so the action names them all.
RUN_ENVS = ("build", "preview", "deploy")


def needs_secret_action(names, *, envs=RUN_ENVS) -> str:
    """What a person does when a run stopped for a secret only they can set."""
    listed = ", ".join(names) or "the missing secret"
    where = ", ".join(envs[:-1]) + f" and {envs[-1]}" if len(envs) > 1 else envs[0]
    return (f"Set {listed} for environments {where} in the app's Secrets, then press Retry "
            "(the coder is not asked to work around a missing secret). The value is given to the "
            "app this build wrote when BOSS starts it, so use a test key.")


def decide(need: Need, *, has_value: bool, purpose: str) -> str:
    """THE ONE RULE, for every runner: "use" | "generate" | "unset" | "needs"."""
    if purpose not in PURPOSES:
        raise ValueError(purpose)
    if need.kind == "generated" and purpose in THROWAWAY:
        return "generate"           # fresh every run, whatever is stored
    if has_value:
        return "use"
    if need.kind == "literal":
        return "literal"            # the app's own setting, unless something bound a value
    if need.kind == "demo":
        # A demo login's password: minted for a throwaway run (shown to a preview's reviewer); a
        # deploy has the person's value or none.
        return "generate" if purpose in THROWAWAY else "unset"
    if need.deploy_only and purpose in THROWAWAY:
        return "unset"              # the deploy's to supply; a throwaway run never waits for it
    if not need.required or need.kind == "listed":
        return "unset"              # the app runs without it, that feature off
    if need.kind == "stub" and purpose in THROWAWAY:
        return "unset"              # the template's test double stands in
    return "needs"


#: A line an app writes when it dies of configuration: an error, a refusal, a missing value.
_ERROR_LINE = re.compile(
    r"error|must be (?:set|provided|defined)|is (?:not set|required|missing|undefined|empty)"
    r"|required|missing|not set|undefined|refus|fatal|invalid|cannot|can't|failed|exception"
    r"|startup_failed", re.IGNORECASE)


def generate_value() -> str:
    return _random.token_urlsafe(GENERATED_BYTES)


def resolve(declared: Mapping[str, Need], *, purpose: str,
            bound: Optional[Mapping[str, str]] = None,
            supplied: Optional[Mapping[str, str]] = None) -> RunEnvironment:
    """The environment for one run. `bound` holds the values a person (or BOSS's issuer) bound for
    the run's environment; a bound name nothing declares is passed as it always was. `supplied`
    is what BOSS gives this launch itself (build services) and wins (RunEnvironment.supply)."""
    bound = {k: v for k, v in (bound or {}).items() if isinstance(v, str) and v}
    env = RunEnvironment(purpose=purpose, declared=dict(declared))
    for name, value in bound.items():
        if name not in declared:
            env.values[name] = value
    for name in sorted(declared):
        need = declared[name]
        verdict = decide(need, has_value=name in bound, purpose=purpose)
        if verdict == "generate":
            env.values[name] = generate_value()
            env.generated.append(name)
            if need.kind == "demo":
                env.shown.append(name)
        elif verdict == "use":
            env.values[name] = bound[name]
        elif verdict == "literal":
            env.values[name] = need.value or ""
        elif verdict == "unset":
            env.unset.append(name)
        else:
            env.needs.append(need)
    if purpose in THROWAWAY and DEMO_SEED_FLAG not in env.values and declares_demo_seeding(declared):
        # The app declares demo seeding (a demo login password, or the flag itself) and nothing
        # set the flag: a throwaway run turns it on, or the seed never runs and the DEMO_PASSWORD
        # BOSS generated and showed the reviewer opens nothing ("invalid credentials"). A value
        # something bound or declared (a literal "false") was taken above and is respected; a
        # deploy's answer is the target's kind (`with_demo_seed`), never decided here.
        env.supply({DEMO_SEED_FLAG: "true"})
    env.supply(names=[n for n in PLATFORM_NAMES if n in declared or n in bound])
    env.supply(supplied)
    if env.generated:
        # A generated value is a secret like any other: kept out of every log line.
        try:
            from ..identity import redaction

            redaction.register_secrets([env.values[n] for n in env.generated])
        except Exception:  # noqa: BLE001 — redaction is a safety net, never a reason to fail
            pass
    return env


# ---- where declarations come from ---------------------------------------------------------------

def merge(*sources: Mapping[str, Need]) -> dict[str, Need]:
    """Earlier sources win a name (a template's spec over a deploy file over .env.example)."""
    out: dict[str, Need] = {}
    for src in sources:
        for name, need in (src or {}).items():
            out.setdefault(name, need)
    return out


def needs_from_specs(specs: Iterable, *, source: str = "the app's template") -> dict[str, Need]:
    """From SecretSpecs (models or dicts): a template's or connector's declared secrets."""
    out: dict[str, Need] = {}
    for spec in specs or ():
        get = spec.get if isinstance(spec, Mapping) else (lambda k, d=None, s=spec: getattr(s, k, d))
        name = get("name")
        if not name:
            continue
        default = str(get("test_default", "none") or "none")
        kind = {"generated": "generated", "stub": "stub"}.get(default, "secret")
        required = get("required", True) is not False
        prior = out.get(str(name))
        if prior is not None:
            required = required or prior.required
        out[str(name)] = Need(str(name), kind, required, source)
    return out


def needs_from_declared_specs(declared: Mapping[str, Mapping]) -> dict[str, Need]:
    """From secrets.declared_specs ({name: {"required", "test_default", "spec"}})."""
    out: dict[str, Need] = {}
    for name, d in (declared or {}).items():
        default = str(d.get("test_default") or "none")
        kind = {"generated": "generated", "stub": "stub"}.get(default, "secret")
        out[name] = Need(name, kind, bool(d.get("required", True)), "the app's template")
    return out


def needs_from_env_example(text: str, *, source: str = ".env.example",
                           label: Optional[str] = None) -> dict[str, Need]:
    """Names only. A credential the file MARKS REQUIRED — the comment lines directly above it or
    its own inline comment say "required" (never "optional" / "not required") — is a secret a
    person must set (ENT-REC-13); everything else keeps the rules in the module docstring."""
    out: dict[str, Need] = {}
    comments: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            comments.append(stripped)
            continue
        m = _ENV_LINE.match(line)
        if not m:
            comments = []       # a blank line ends the comment block above a name
            continue
        name = m.group(1)
        said = " ".join(comments + ([line.split("#", 1)[1]] if "#" in line else []))
        comments = []
        kind = ("generated" if _BOSS_OWNED_NAME.match(name)
                else "demo" if is_demo_login_name(name) else "listed")
        if (kind == "listed" and is_person_only_name(name) and _REQUIRED_MARK.search(said)
                and not _NOT_REQUIRED_MARK.search(said)):
            whose = f"{label}/{source}" if label else source
            out.setdefault(name, Need(name, "secret", True, f"{whose}, which marks it required"))
            continue
        out.setdefault(name, Need(name, kind, kind == "generated", source))
    return out


def needs_from_deploy_manifest(manifest, *, source: str = "boss.deploy.yaml") -> dict[str, Need]:
    """A generated DEMO LOGIN password is a demo login (shown to the reviewer, kept for every
    preview), as it is in .env.example; a plain literal (`ALLOW_DEMO_SEED: "true"`, no `{…}` to
    fill in) is the app's own setting and every run of it gets it. HelpDesk AI's preview got
    neither: its demo seed never ran and nobody could sign in (prev_e3a41482afaa4b228abe)."""
    out: dict[str, Need] = {}
    for item in getattr(manifest, "env", ()) or ():
        if item.source == "generate":
            out[item.key] = Need(item.key, "demo" if is_demo_login_name(item.key) else "generated",
                                 True, source)
        elif item.source == "literal" and "{" not in str(item.value or ""):
            out[item.key] = Need(item.key, "literal", False, source, value=str(item.value or ""))
        elif item.source == "secret":
            # A DEPLOY's value (its DATABASE_URL, a live key): a throwaway run never waits for it.
            out[item.key] = Need(item.key, "secret", True, source, deploy_only=True)
    return out


def needs_from_app_dir(app_dir: Optional[Path], *, label: Optional[str] = None) -> dict[str, Need]:
    """What the app's own files declare: its boss.deploy.yaml, then its .env.example. Never
    raises: an unreadable file declares nothing. `label` names the app in each source
    ("helpdesk/.env.example"), so a card says whose file asked."""
    if app_dir is None:
        return {}
    found: list[dict[str, Need]] = []
    deploy_file = app_dir / "boss.deploy.yaml"
    if deploy_file.is_file():
        try:
            from ..services.app_deploy_manifest import load_manifest

            found.append(needs_from_deploy_manifest(load_manifest(deploy_file)))
        except Exception:  # noqa: BLE001
            pass
    example = app_dir / ".env.example"
    if example.is_file():
        try:
            found.append(needs_from_env_example(example.read_text(errors="ignore"), label=label))
        except OSError:
            pass
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        compose = app_dir / name
        if compose.is_file():
            found.append(needs_from_compose(compose))
            break
    out = merge(*found)
    # A REQUIRED MARK OUTRANKS "the deploy's". boss.deploy.yaml `{secret}` alone is the deploy's
    # value (a throwaway run never waits for it); the same name marked required in .env.example
    # says the app does not start without it, so every run needs it (ENT-REC-13).
    for name, need in list(out.items()):
        marked = next((src[name] for src in found if name in src
                       and src[name].person_must_supply()), None)
        if need.deploy_only and marked is not None:
            out[name] = marked
    return out


def needs_from_compose(path: Path) -> dict[str, Need]:
    """The names a compose file's services take from the environment (`SESSION_SECRET:
    ${SESSION_SECRET}`) that are BOSS's to mint: a session/signing key, generated; a demo login's
    password, a demo login. Everything else there is the deploy's business, not a preview's.

    HelpDesk AI's compose file reads SESSION_SECRET, which nothing else declares: its preview
    died on "SESSION_SECRET must be set ... (BOSS generates one)" (prev_ec5be7a0201a47ad9143),
    while an earlier build had passed only because its coder made the app mint its own."""
    try:
        import yaml

        doc = yaml.safe_load(path.read_text(errors="ignore")) or {}
    except Exception:  # noqa: BLE001 — an unreadable compose file declares nothing
        return {}
    out: dict[str, Need] = {}
    services = doc.get("services") if isinstance(doc, Mapping) else None
    for svc in (services or {}).values() if isinstance(services, Mapping) else ():
        env = svc.get("environment") if isinstance(svc, Mapping) else None
        if isinstance(env, Mapping):
            names = [str(k) for k in env]
        elif isinstance(env, list):
            names = [str(e).split("=", 1)[0] for e in env]
        else:
            names = []
        for name in names:
            if _BOSS_OWNED_NAME.match(name):
                out.setdefault(name, Need(name, "generated", True, path.name))
            elif is_demo_login_name(name):
                out.setdefault(name, Need(name, "demo", False, path.name))
    return out


def app_needs(c, *, project_id: str, app_key: Optional[str], app_dir: Optional[Path] = None,
              manifests: Iterable = ()) -> dict[str, Need]:
    """Everything the app declares: its applied templates (by app key), any template manifest in
    hand (the build's, before its application row is read), then its own files."""
    from .secrets import declared_specs

    sources: list[dict[str, Need]] = []
    if app_key:
        sources.append(needs_from_declared_specs(
            declared_specs(c, project_id=project_id, app_key=app_key)))
    for m in manifests or ():
        specs = m.get("secrets") if isinstance(m, Mapping) else getattr(m, "secrets", ())
        sources.append(needs_from_specs(specs or ()))
    sources.append(needs_from_app_dir(app_dir))
    return merge(*sources)
