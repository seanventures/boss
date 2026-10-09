"""The `boss-template.yaml` manifest, schema `boss.template/v1`. FROZEN.

Spec: docs/product/APP_TEMPLATES_AND_SECURITY_LOOP.md §2.2. One schema, used at import, at apply
and in CI for the built-in templates (`templates/apps/<id>/boss-template.yaml`).

FROZEN on day 1 (stream S0): every stream builds against it. A change is a new schema id
(`boss.template/v2`), not an edit; `test_template_manifest.py` pins the field set.

    manifest = load_manifest(Path("templates/apps/payments-stripe/boss-template.yaml"))
    manifest = validate_manifest(yaml_text_or_dict)          # raises ManifestError

`ManifestError.reasons` lists every problem, one line each, as `<field path>: <what is wrong>`,
so an import's validation report (§2.5) can show them all at once.

What the schema checks is the manifest alone. The import's other checks (the file tree, sizes,
symlinks, the secret scan, an id taken by a built-in, BOSS's own hosts in `egress`, who may set
`insecure_by_design`) need the repository or the install and live in S2's validator.
"""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SCHEMA_ID = "boss.template/v1"

#: The stacks a template may declare (F1 toolchains). One framework per stack is tested in v1 (D2).
Stack = Literal["node", "python", "java"]
STACKS: tuple[str, ...] = ("node", "python", "java")

TEMPLATE_ID = r"^[a-z0-9-]{3,48}$"
#: Semantic version, MAJOR.MINOR.PATCH with optional pre-release and build (semver.org §2, §9, §10).
SEMVER = (r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
          r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
          r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$")
#: A word in a closed-vocabulary list (`adds`, `labels`, a framework, a toolchain, a provider).
WORD = r"^[a-z][a-z0-9_-]{0,47}$"
#: An environment variable name the app receives (a secret or a plain setting).
ENV_NAME = r"^[A-Z][A-Z0-9_]{0,63}$"
#: `METHOD /path` as the app serves it; the template's tests are written against these (§2.11).
ROUTE = r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) /[^\s]*$"
_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

#: The label that marks a deliberately insecure template (F3 §3.6); only an importer holding
#: template:approve may set it, which the import checks (§2.5), not this schema.
INSECURE_BY_DESIGN = "insecure_by_design"


class ManifestError(ValueError):
    """A manifest that is not valid `boss.template/v1`. `reasons` has one line per problem."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("invalid boss-template.yaml:\n" + "\n".join(f"  - {r}" for r in self.reasons))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class StackSpec(_Strict):
    frameworks: tuple[str, ...] = Field(min_length=1)
    min_toolchain: str = Field(pattern=WORD)

    @field_validator("frameworks")
    @classmethod
    def _frameworks(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for name in v:
            if not re.match(WORD, name):
                raise ValueError(f"framework {name!r} must match {WORD}")
        if len(set(v)) != len(v):
            raise ValueError("a framework is listed twice")
        return v


def _check_regex(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    try:
        re.compile(v)
    except re.error as exc:
        raise ValueError(f"is not a valid regular expression ({exc})") from None
    return v


class SecretSpec(_Strict):
    name: str = Field(pattern=ENV_NAME)
    purpose: Optional[str] = Field(default=None, max_length=200)
    #: none = the operator must paste a test value; generated = BOSS makes one; stub = the
    #: template's test double is used (§2.4).
    test_default: Literal["none", "generated", "stub"] = "none"
    #: What build, preview and tests accept.
    pattern: Optional[str] = Field(default=None, max_length=500)
    #: Accepted only for env=deploy, after a person's decision (§2.4).
    live_pattern: Optional[str] = Field(default=None, max_length=500)

    @field_validator("pattern", "live_pattern")
    @classmethod
    def _regexes(cls, v: Optional[str]) -> Optional[str]:
        return _check_regex(v)


class EnvSpec(_Strict):
    name: str = Field(pattern=ENV_NAME)
    default: Optional[Union[str, int, float, bool]] = None


class WebhookSpec(_Strict):
    path: str = Field(pattern=r"^/[^\s]*$", max_length=200)
    provider: str = Field(pattern=WORD)


class TestSpec(_Strict):
    __test__ = False  # not a pytest class
    command: str = Field(min_length=1, max_length=500)
    report: Literal["junit"]
    min_tests: int = Field(ge=1)


def _relative_file(v: str) -> str:
    p = PurePosixPath(v)
    if not v or v.startswith("/") or "\\" in v or ".." in p.parts or p.parts[:1] == ("~",):
        raise ValueError("must be a relative path inside the template (no '/', '..' or '~' prefix)")
    return v


def _hostname(v: str) -> str:
    host = v.strip().lower().rstrip(".")
    if "*" in host:
        raise ValueError(f"{v!r}: wildcards are not allowed; list each host")
    if "/" in host or ":" in host or "@" in host:
        raise ValueError(f"{v!r}: a bare hostname only (no scheme, port, path or user)")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError(f"{v!r}: an IP address is not allowed; use a hostname")
    labels = host.split(".")
    if len(labels) < 2 or not all(_HOST_LABEL.match(label) for label in labels) or len(host) > 253:
        raise ValueError(f"{v!r}: not a fully qualified hostname")
    if labels[-1] in {"localhost", "local", "internal", "lan", "home", "corp", "test", "invalid"} \
            or labels[-1].isdigit():
        raise ValueError(f"{v!r}: a local or reserved name is not an egress host")
    return host


class TemplateManifest(_Strict):
    """`boss-template.yaml`, schema `boss.template/v1` (§2.2)."""

    schema_: Literal["boss.template/v1"] = Field(alias="schema")
    id: str = Field(pattern=TEMPLATE_ID)
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(pattern=SEMVER)
    summary: str = Field(min_length=1, max_length=200)
    adds: tuple[str, ...] = ()
    stacks: dict[Stack, StackSpec] = Field(min_length=1)
    requires: tuple[str, ...] = ()
    works_with: tuple[str, ...] = ()
    secrets: tuple[SecretSpec, ...] = ()
    env: tuple[EnvSpec, ...] = ()
    #: Hosts the RUNNING app needs: shown before apply, approved at apply (§2.9 S2.2).
    egress: tuple[str, ...] = ()
    webhooks: tuple[WebhookSpec, ...] = ()
    routes: tuple[str, ...] = ()
    tests: dict[Stack, TestSpec] = Field(min_length=1)
    security_note: str = "SECURITY.md"
    guide: str = "guide.md"
    labels: tuple[str, ...] = ()
    license: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.+()-]*( (AND|OR|WITH) [A-Za-z0-9][A-Za-z0-9.+()-]*)*$",
                         max_length=100)
    min_boss: str = Field(pattern=SEMVER)

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, populate_by_name=True)

    @field_validator("adds", "labels")
    @classmethod
    def _words(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for word in v:
            if not re.match(WORD, word):
                raise ValueError(f"{word!r} must match {WORD}")
        if len(set(v)) != len(v):
            raise ValueError("an entry is listed twice")
        return v

    @field_validator("requires", "works_with")
    @classmethod
    def _template_ids(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for tid in v:
            if not re.match(TEMPLATE_ID, tid):
                raise ValueError(f"{tid!r} is not a template id ({TEMPLATE_ID})")
        if len(set(v)) != len(v):
            raise ValueError("a template is listed twice")
        return v

    @field_validator("egress")
    @classmethod
    def _egress(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        hosts = tuple(_hostname(h) for h in v)
        if len(set(hosts)) != len(hosts):
            raise ValueError("a host is listed twice")
        return hosts

    @field_validator("routes")
    @classmethod
    def _routes(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for route in v:
            if not re.match(ROUTE, route):
                raise ValueError(f"{route!r} must be 'METHOD /path' (e.g. 'POST /api/checkout')")
        if len(set(v)) != len(v):
            raise ValueError("a route is listed twice")
        return v

    @field_validator("security_note", "guide")
    @classmethod
    def _files(cls, v: str) -> str:
        return _relative_file(v)

    @model_validator(mode="after")
    def _consistent(self) -> "TemplateManifest":
        problems = []
        missing = [s for s in self.stacks if s not in self.tests]
        if missing:
            problems.append(f"tests: every declared stack needs a test command; missing {missing}")
        extra = [s for s in self.tests if s not in self.stacks]
        if extra:
            problems.append(f"tests: {extra} not declared under stacks")
        if self.id in self.requires or self.id in self.works_with:
            problems.append("requires/works_with: a template cannot name itself")
        both = set(self.requires) & set(self.works_with)
        if both:
            problems.append(f"works_with: {sorted(both)} already in requires")
        for field in ("secrets", "env"):
            names = [item.name for item in getattr(self, field)]
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                problems.append(f"{field}: {dupes} listed twice")
        clash = sorted({s.name for s in self.secrets} & {e.name for e in self.env})
        if clash:
            problems.append(f"env: {clash} is also a secret; a value is one or the other")
        paths = [w.path for w in self.webhooks]
        if len(set(paths)) != len(paths):
            problems.append("webhooks: a path is listed twice")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def insecure_by_design(self) -> bool:
        return INSECURE_BY_DESIGN in self.labels

    def secret(self, name: str) -> Optional[SecretSpec]:
        return next((s for s in self.secrets if s.name == name), None)


#: What a pattern means, for the error message (the raw regex is still quoted after it).
_PATTERN_MEANING = {
    TEMPLATE_ID: "must be 3-48 characters of a-z, 0-9 and '-'",
    SEMVER: "must be a semantic version like 1.0.0",
    WORD: "must be a lowercase word (a-z, 0-9, '_', '-')",
    ENV_NAME: "must be an environment variable name like STRIPE_SECRET_KEY",
    r"^/[^\s]*$": "must be an absolute URL path like /webhooks/stripe",
}


def _reasons(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        loc = ".".join("schema" if part == "schema_" else str(part) for part in err["loc"])
        is_key = bool(err["loc"]) and err["loc"][-1] == "[key]"
        loc = loc.replace(".[key]", "")
        msg = err["msg"].removeprefix("Value error, ")
        if err["type"] == "string_pattern_mismatch":
            pattern = (err.get("ctx") or {}).get("pattern", "")
            meaning = _PATTERN_MEANING.get(pattern)
            msg = f"{meaning} (pattern {pattern})" if meaning and pattern != SEMVER else (meaning or msg)
        elif err["type"] == "extra_forbidden":
            msg = "unknown field (boss.template/v1 does not define it)"
        elif err["type"] == "missing":
            msg = "is required"
        elif err["type"] == "literal_error" and loc == "schema":
            msg = f"must be {SCHEMA_ID!r}"
        elif err["type"] == "literal_error" and is_key:
            msg = f"unknown stack; must be one of {', '.join(STACKS)}"
        out.append(f"{loc}: {msg}" if loc else msg)
    return out


def validate_manifest(source: Union[str, bytes, dict[str, Any]]) -> TemplateManifest:
    """Parse and validate a manifest (YAML text or an already-parsed mapping)."""
    data: Any = source
    if isinstance(source, (str, bytes)):
        try:
            data = yaml.safe_load(source)
        except yaml.YAMLError as exc:
            raise ManifestError([f"not valid YAML: {exc}"]) from None
    if not isinstance(data, dict):
        raise ManifestError(["the manifest must be a YAML mapping"])
    try:
        return TemplateManifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(_reasons(exc)) from None


def load_manifest(path: Union[str, Path]) -> TemplateManifest:
    """Read and validate `boss-template.yaml` (pass the file, or the template's directory)."""
    p = Path(path)
    if p.is_dir():
        p = p / "boss-template.yaml"
    return validate_manifest(p.read_text(encoding="utf-8"))
