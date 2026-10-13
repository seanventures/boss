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
  * its boss.deploy.yaml `env:` entries — `{generate: ...}` BOSS mints, `{secret: ...}` a person sets;
  * its .env.example names. Only a session/signing-key name there is BOSS's to mint (SESSION_SECRET,
    JWT_SECRET, SECRET_KEY, ...); every other name is "listed": never blocks, but when the app dies
    naming it, the verdict says the app needs it instead of showing a bare crash.

THE RULES (`decide`):
  * generated — a throwaway run (proof, preview, smoke) gets a FRESH random value every time; it is
    never stored and never reused, so a deploy can never inherit it. A deploy uses its own stored
    deploy value (ensure_deploy_generated makes it once).
  * stub / optional — left unset when nothing is bound, so the app runs without that provider:
    exactly what a deploy does with a `required: false` secret.
  * a required secret a person must set — its bound value when there is one; otherwise the run is
    not started and the verdict names the variable ("needs a secret"), never a crash.
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
    r"|^(?:APP_)?SECRET_KEY(?:_BASE)?$")

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

#: How long a generated throwaway value is: token_urlsafe(32) is 43 characters, past every
#: "at least 32 characters" check an app makes.
GENERATED_BYTES = 32


@dataclass(frozen=True)
class Need:
    """One variable the app declares. `kind`: generated | secret | stub | listed."""
    name: str
    kind: str
    required: bool
    source: str

    def where(self) -> str:
        return f"declared by {self.source}"


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

    def verdict(self) -> Optional[str]:
        """"needs a secret: STRIPE_SECRET_KEY (declared by template payments)" or None."""
        if not self.needs:
            return None
        named = ", ".join(f"{n.name} ({n.where()})" for n in self.needs)
        return f"needs a secret: {named}. BOSS cannot make it; set it in the app's Secrets"

    def evidence(self) -> dict:
        """Names only, for a record or an event."""
        return {"generated": list(self.generated), "unset": list(self.unset),
                "needs_secret": [n.name for n in self.needs]}

    def unset_named_in(self, log_text: str) -> Optional[str]:
        """The declared variable this run was not given that the app's log names, if any."""
        text = log_text or ""
        for name in self.unset:
            if re.search(rf"\b{re.escape(name)}\b", text):
                return name
        return None

    def explain_crash(self, log_text: str) -> Optional[str]:
        """When the app died naming a declared variable it was not given, say THAT, by name."""
        name = self.unset_named_in(log_text)
        if name is None:
            return None
        need = self.declared.get(name)
        where = need.where() if need else "named in its start log"
        return (f"needs a secret: {name} ({where}). BOSS did not give it to this run; set it "
                "in the app's Secrets, or make the app start without it")


def decide(need: Need, *, has_value: bool, purpose: str) -> str:
    """THE ONE RULE, for every runner: "use" | "generate" | "unset" | "needs"."""
    if purpose not in PURPOSES:
        raise ValueError(purpose)
    if need.kind == "generated" and purpose in THROWAWAY:
        return "generate"           # fresh every run, whatever is stored
    if has_value:
        return "use"
    if not need.required or need.kind == "listed":
        return "unset"              # the app runs without it, that feature off
    if need.kind == "stub" and purpose in THROWAWAY:
        return "unset"              # the template's test double stands in
    return "needs"


def generate_value() -> str:
    return _random.token_urlsafe(GENERATED_BYTES)


def resolve(declared: Mapping[str, Need], *, purpose: str,
            bound: Optional[Mapping[str, str]] = None) -> RunEnvironment:
    """The environment for one run. `bound` holds the values a person (or BOSS's issuer) bound for
    the run's environment; a bound name nothing declares is passed as it always was."""
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
        elif verdict == "use":
            env.values[name] = bound[name]
        elif verdict == "unset":
            env.unset.append(name)
        else:
            env.needs.append(need)
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


def needs_from_env_example(text: str, *, source: str = ".env.example") -> dict[str, Need]:
    out: dict[str, Need] = {}
    for line in (text or "").splitlines():
        m = _ENV_LINE.match(line)
        if not m:
            continue
        name = m.group(1)
        kind = "generated" if _BOSS_OWNED_NAME.match(name) else "listed"
        out.setdefault(name, Need(name, kind, kind == "generated", source))
    return out


def needs_from_deploy_manifest(manifest, *, source: str = "boss.deploy.yaml") -> dict[str, Need]:
    out: dict[str, Need] = {}
    for item in getattr(manifest, "env", ()) or ():
        if item.source == "generate":
            out[item.key] = Need(item.key, "generated", True, source)
        elif item.source == "secret":
            out[item.key] = Need(item.key, "secret", True, source)
    return out


def needs_from_app_dir(app_dir: Optional[Path]) -> dict[str, Need]:
    """What the app's own files declare: its boss.deploy.yaml, then its .env.example. Never
    raises: an unreadable file declares nothing."""
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
            found.append(needs_from_env_example(example.read_text(errors="ignore")))
        except OSError:
            pass
    return merge(*found)


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
