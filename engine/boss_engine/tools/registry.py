"""The engine tool layer — registry, specs and schema emission.

Every engine capability the BOSS Agent (or an external MCP client) can use is a
`ToolSpec`: a name, a human description, a JSON-Schema for its arguments, a
`kind` ("read" or "write"), and a `handler`. The handler is a plain Python
function that receives the `EngineContext` and the parsed arguments and returns
a JSON-serializable dict — it calls engine helpers in-process, never HTTP.

One registry, two consumers:
  - `registry.openai_schema()` feeds the engine-native agent's tool-calling loop
    (OpenAI/Anthropic "type: function" shape).
  - `registry.mcp_schema()` feeds the MCP server (`name`/`description`/
    `inputSchema` shape) so GitHub/Claude agents can drive the engine.

`kind` matters: "read" tools are always safe to call; "write" tools create or
mutate workflow state (start a code task, launch a preview) and callers may
require confirmation before invoking them.
"""
from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal, Optional

ToolKind = Literal["read", "write"]

#: WHAT PRESSING THIS COSTS IF IT WAS THE WRONG CALL.
#:
#: `kind` says whether a tool changes anything. It does not say whether the change can be taken
#: back, and that is the question the agent loop actually needs: `retarget_app` and `cancel_job`
#: are both writes, and one of them threw away twenty-seven minutes of a build because nothing
#: distinguished them. The membership lived in two hand-maintained name sets in the agent, so a
#: tool added tomorrow was silently in neither.
#:
#:   safe          — changes state, and the change can be undone or simply redone.
#:   costs_money   — spends real budget per call. Reversible in state, not in spend.
#:   destroys_work — throws away work in flight, or removes a control somebody put there.
#:   irreversible  — cannot be undone by any means BOSS has.
#:   grants_authority — hands an agent reach it did not have, on somebody's account: an MCP
#:                   server carries that server's credentials and every action it exposes.
#:                   The grant can be taken back; what the agent did with it in between cannot.
#:                   Added when assign_mcp_to_agent shipped as `safe`, so the chat model could
#:                   give any agent any registered server on its own.
#:
#: `destroys_work`, `irreversible` and `grants_authority` are never executed by the model. They
#: are offered.
Reversibility = Literal["safe", "costs_money", "destroys_work", "irreversible",
                        "grants_authority"]

#: The ones the model may never press itself, whatever it was asked.
NEVER_SELF_EXECUTE: frozenset[str] = frozenset({"destroys_work", "irreversible",
                                                "grants_authority"})

#: WHAT THE OPERATOR READS ON A BUTTON THE MODEL MAY NOT PRESS ITSELF — one true sentence per
#: reversibility class. Before this existed, a `costs_money` tool that is `operator_only`
#: (request_self_fix: it only asks BOSS to diagnose and propose a diff a person still approves)
#: fell through to the destructive wording and told the operator it "destroys work or removes a
#: safeguard". Every class has its own entry (a test walks `Reversibility`), so no class ever
#: borrows another's warning.
REVERSIBILITY_WARNINGS: dict[str, str] = {
    "safe": "changes state that can be undone or simply redone",
    "costs_money": "spends model budget when it runs",
    "destroys_work": "throws away work in flight, or removes a control",
    "irreversible": "cannot be undone once done",
    "grants_authority": "gives an agent reach on somebody's account",
}


def offer_warning(spec: "ToolSpec") -> str:
    """Why this tool is offered as a button instead of run: its class's warning, the tool's own
    true follow-on (`offer_note`, e.g. "nothing installs without a person's approval"), then who
    decides. The one wording the agent's offer and the registry's refusal both use."""
    warning = REVERSIBILITY_WARNINGS.get(spec.reversibility) or "changes state"
    note = (spec.offer_note or "").strip()
    return f"{warning}{'; ' + note if note else ''} — the operator decides"


# handler(ctx, *, project_id, actor, args) -> dict
ToolHandler = Callable[..., dict]

#: The surfaces a tool can be offered on (HARNESS_KERNEL_DESIGN.md §5.1).
SURFACES = ("chat", "agent_step", "decision", "build", "mcp")


@dataclass(frozen=True)
class Exposure:
    """WHERE A TOOL IS RELEVANT BY DEFAULT (K5, design §5.1). Grants stay where they are
    (role_skills / role_mcp_servers hold what an agent MAY use); Exposure says where a tool is
    worth a model's context. The offered set is grant ∩ surface ∩ (core ∪ found): a kernel run
    with BOSS_SCOPED_TOOLS on (kernel/scoped_tools.py) sees the surface's `core_for` tools plus
    `find_tools`, and every other surface-eligible tool is DEFERRED — found by name/description
    search, then offered from the next step on. With the flag off nothing changes.

    Conservative by construction: `surfaces` defaults to everything chat and MCP see today, so
    declaring an Exposure never takes a tool away from a surface that has it — only `core_for`
    (what rides in every call's schema block) is the scarce set, capped by the lint test
    (test_tool_exposure.py).

      surfaces      where the tool may be offered at all: chat | agent_step | decision | build | mcp
      tags          one or two words find_tools ranks on ("builds", "deploy", "kg", ...)
      core_for      surfaces that always see it un-deferred (chat core ≤20, lint-capped)
      stages        build stage nodes allowed to see it (empty = none; used from K12)
      search_hint   extra words for find_tools ranking
    """

    surfaces: frozenset = frozenset({"chat", "mcp"})
    tags: frozenset = frozenset()
    core_for: frozenset = frozenset()
    stages: frozenset = frozenset()
    search_hint: str = ""

    def __post_init__(self) -> None:
        # Accept any iterable of strings; store frozensets, so Exposure stays hashable and a
        # registration reads `exposure=Exposure(tags=("builds",), core_for=("chat",))`.
        for field_name in ("surfaces", "tags", "core_for", "stages"):
            value = getattr(self, field_name)
            if not isinstance(value, frozenset):
                object.__setattr__(self, field_name, frozenset(value))
        unknown = (self.surfaces | self.core_for) - frozenset(SURFACES)
        if unknown:
            raise ValueError(f"unknown surface(s) {sorted(unknown)}; the surfaces are {SURFACES}")
        if not self.core_for <= self.surfaces:
            raise ValueError("core_for must name surfaces the tool is exposed on")


def strict_parameters(schema: Optional[dict]) -> dict:
    """THE STRICT FORM of a tool's JSON schema (K5, design §5.3, §9.5): every property listed in
    `required`, originally-optional properties made nullable (type gains "null"; an enum gains
    None), `additionalProperties: false` at every object level, and `default`/`format` dropped
    (the provider strict subsets reject them). The original schema is never mutated.

    Handlers already treat a missing optional as None, and the derivation test
    (test_tool_exposure.py) holds that calls valid under the original schema stay valid under
    the strict one once their omitted optionals are written as null — across EVERY registry
    tool, so a tool whose handler distinguishes "missing" from null is found before K7 flips
    strict mode on."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "required": [],
                "additionalProperties": False}
    return _strictify(schema, make_nullable=False)


def _strictify(schema: dict, *, make_nullable: bool) -> dict:
    out = {k: v for k, v in schema.items() if k not in ("default", "format")}
    if make_nullable:
        declared = out.get("type")
        if isinstance(declared, str) and declared != "null":
            out["type"] = [declared, "null"]
        elif isinstance(declared, list) and "null" not in declared:
            out["type"] = list(declared) + ["null"]
        if isinstance(out.get("enum"), list) and None not in out["enum"]:
            out["enum"] = list(out["enum"]) + [None]
    properties = out.get("properties")
    if isinstance(properties, dict):
        required = {str(r) for r in (out.get("required") or [])}
        out["properties"] = {
            name: _strictify(prop, make_nullable=name not in required)
            if isinstance(prop, dict) else prop
            for name, prop in properties.items()}
        out["required"] = sorted(properties)
        out["additionalProperties"] = False
    if isinstance(out.get("items"), dict):
        out["items"] = _strictify(out["items"], make_nullable=False)
    return out


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema (object)
    handler: ToolHandler
    kind: ToolKind = "read"
    #: Only meaningful for writes. Reads change nothing, so they are `safe` by construction.
    reversibility: Reversibility = "safe"
    #: API-backed tools use the API's durable control plane. Native engine
    #: writes still require the local Postgres workspace.
    requires_durable_workspace: bool = True
    #: THE CATALOGUE PERMISSION THIS TOOL NEEDS (docs/product/IDENTITY.md §6.4). Required: a spec
    #: with neither a permission nor `inherits_route_permission` cannot be built, and the key is
    #: checked against packages/identity/catalogue.yaml then, so a misspelt key fails at import.
    #: `invoke` checks it with authz.check before the handler runs.
    permission: Optional[str] = None
    #: True for the API-surface tools: each one calls an API route, whose own declaration is
    #: checked when the call is dispatched, so the tool cannot drift from the route.
    inherits_route_permission: bool = False
    #: Ids in the arguments that must belong to the call's project (object_rules.Confine at
    #: `args:`), and where a deploying tool's target comes from (object_rules.Production). Filled
    #: from tools/object_args.py when the tool is registered; `identity_gate` enforces both.
    confine: tuple = ()
    production: Any = None
    #: True for a project-PINNED registry (the discuss/chat registry): the call acts in the
    #: session's project, whatever `project_id` the arguments carry. `invoke` strips the argument
    #: BEFORE the identity gate, so the gate decides the project the handler acts in (H1).
    pinned_project: bool = False
    #: The tool acts ACROSS the install, not in one project, so its permission is checked at ORG
    #: scope (a project binding is not enough): True always, or the name of a boolean argument
    #: that widens the call to every project (mandate_standard's `all_projects`). M6/M7.
    org_scope: Any = False
    #: An OPERATION-dependent permission: (argument name, ((value, permission), ...)). A tool
    #: whose `action` argument picks between operations that have different permissions over
    #: HTTP (decide: approve vs delegate vs execute) needs the chosen operation's permission,
    #: not one key for all of them. A value not listed takes `permission`.
    permission_by_arg: tuple = ()
    #: FURTHER permissions the call also needs, each checked at its own scope (an org-only key at
    #: the org, the rest at the acting project) after `permission` is allowed. A route that checks a
    #: second key in its handler (apply_template: workflow_run:start at the project AND
    #: template:read, §2.6) is mirrored here, so the tool cannot be weaker than its route.
    also_requires: tuple = ()
    #: OFFERED, NEVER PRESSED BY THE MODEL — whatever its reversibility. For a tool that applies
    #: a batch of moves the person did not see one by one (the captain's overwatch and drive,
    #: which can start a factory build): plain text never sets it off; the agent offers it, and
    #: a person's press (or typed /overwatch) runs it (BOSSAPP-759).
    operator_only: bool = False
    #: A WRITE THAT ONLY QUEUES SOMETHING FOR A PERSON. It records a proposal that changes nothing
    #: until a person acts on it (a platform gap filed for BOSS's developers, BOSSAPP-571), so it
    #: runs inside the turn that found it, even a question's: the finding is written down the
    #: moment it exists, and nothing else moves. Must be `safe` and never `operator_only`.
    queues_for_review: bool = False
    #: A TRUE FOLLOW-ON TO THE OFFER'S WARNING, owned by the tool ("nothing installs without a
    #: person's approval of the diff"). Optional; see `offer_warning`.
    offer_note: str = ""
    #: WHERE THIS TOOL IS RELEVANT (K5, design §5.1): declared at the registration, next to the
    #: tool, never in a separate list. Required in practice: the lint test
    #: (test_tool_exposure.py) fails any registered tool that leaves it None.
    exposure: Optional[Exposure] = None
    #: WHAT A RESUMED RUN MAY DO WITH THIS CALL IF ITS OUTCOME WAS NEVER RECORDED (AP-P2,
    #: kernel/idempotency.py): idempotent | keyed | irreversible. A write takes its class from
    #: kernel.idempotency.TOOL_IDEMPOTENCY when the registration names none; a write in neither
    #: is a lint failure (test_every_write_tool_declares_an_idempotency_class), and the kernel
    #: reads silence as irreversible.
    idempotency: Optional[str] = None
    #: ARGUMENTS WHOSE SCHEMA LIVES ELSEWHERE. True for a tool whose valid top-level arguments
    #: also include ones only the target declares (start_workflow: the workflow version's
    #: input_schema, brec_6025901347b6445eb9c1). `schema_args` lets the unlisted NON-underscore
    #: keys through, and the handler keeps only what that schema declares and names the rest
    #: in `ignored_args`. BOSS's underscore keys are still never a caller's.
    handler_checks_extra_args: bool = False

    def permission_for(self, args: Optional[dict]) -> Optional[str]:
        """The permission THIS call needs (see `permission_by_arg`)."""
        if self.permission_by_arg:
            name, table = self.permission_by_arg
            chosen = dict(table).get(str((args or {}).get(name) or ""))
            if chosen:
                return chosen
        return self.permission

    def acts_org_wide(self, args: Optional[dict]) -> bool:
        if self.org_scope is True:
            return True
        if isinstance(self.org_scope, str) and self.org_scope:
            return bool((args or {}).get(self.org_scope))
        return False

    def describe(self) -> str:
        return f"tool {self.name} {self.permission or 'route'}"

    def __post_init__(self) -> None:
        from ..identity.catalogue import CatalogueError, require_known

        if self.permission is not None:
            require_known(self.permission)
            for _value, key in (self.permission_by_arg[1] if self.permission_by_arg else ()):
                require_known(key)
            for key in self.also_requires:
                require_known(key)
        elif self.also_requires:
            raise CatalogueError(f"tool {self.name!r}: also_requires needs a permission= first")
        elif not self.inherits_route_permission:
            raise CatalogueError(f"tool {self.name!r} declares no permission: pass permission= "
                                 "(a catalogue key) or inherits_route_permission=True")
        from ..kernel import idempotency as _idempotency

        if self.kind == "write" and self.idempotency is None:
            object.__setattr__(self, "idempotency", _idempotency.declared(self.name))
        _idempotency.validate(self.idempotency, tool=self.name)
        if self.queues_for_review and (self.kind != "write" or self.reversibility != "safe"
                                       or self.operator_only):
            raise ValueError(f"tool {self.name!r}: queues_for_review is for a safe write a "
                             "person reviews, never for one that is operator_only")

    @property
    def self_executable(self) -> bool:
        """May the model run this itself, given permission to write?"""
        return self.reversibility not in NEVER_SELF_EXECUTE and not self.operator_only

    def openai(self) -> dict:
        """OpenAI/Anthropic function-calling schema (used by the agent loop)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def mcp(self) -> dict:
        """MCP tools/list schema (used by the MCP server)."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters,
        }

    def strict_schema(self) -> dict:
        """This tool's parameters in the provider strict form (see `strict_parameters`)."""
        return strict_parameters(self.parameters)


class ToolError(Exception):
    """Raised when a tool is unknown or its handler fails cleanly."""


@dataclass
class ToolRegistry:
    _tools: dict[str, ToolSpec] = field(default_factory=dict)

    # ---- registration -----------------------------------------------------
    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        if not spec.confine and spec.production is None:
            from .object_args import rules_for

            confine, production = rules_for(spec.name)
            if confine or production is not None:
                spec = replace(spec, confine=confine, production=production)
        self._tools[spec.name] = spec
        return spec

    def tool(
        self,
        name: str,
        description: str,
        parameters: Optional[dict] = None,
        *,
        kind: ToolKind = "read",
        reversibility: Optional[Reversibility] = None,
        permission: Optional[str] = None,
        org_scope: Any = False,
        permission_by_arg: tuple = (),
        also_requires: tuple = (),
        operator_only: bool = False,
        queues_for_review: bool = False,
        offer_note: str = "",
        exposure: Optional[Exposure] = None,
        idempotency: Optional[str] = None,
        handler_checks_extra_args: bool = False,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """Decorator form: @registry.tool("list_x", "…", {...}, kind="read")."""

        def deco(fn: ToolHandler) -> ToolHandler:
            self.register(
                ToolSpec(
                    name=name,
                    description=description,
                    parameters=parameters or _empty_object_schema(),
                    handler=fn,
                    kind=kind,
                    # A read changes nothing, so it needs no declaration. A write that does not
                    # declare defaults to `safe` — and a registry test requires every write to
                    # say so on purpose, so the default can never quietly cover a new
                    # destructive tool.
                    reversibility=reversibility or "safe",
                    permission=permission,
                    org_scope=org_scope,
                    permission_by_arg=permission_by_arg,
                    also_requires=tuple(also_requires),
                    operator_only=operator_only,
                    queues_for_review=queues_for_review,
                    offer_note=offer_note,
                    exposure=exposure,
                    idempotency=idempotency,
                    handler_checks_extra_args=handler_checks_extra_args,
                )
            )
            return fn

        return deco

    # ---- lookup / listing -------------------------------------------------
    #: Namespace prefixes some models put in front of a tool name. OpenAI's legacy function
    #: calling used `functions.foo`, and models trained on it still emit `functions.foo` or
    #: `functions/foo` — observed live as `functions/read_logs`, which Groq rejected outright.
    #: Where a provider does NOT validate, the malformed name reaches us instead, and failing
    #: it as "unknown tool" would blame the operator for a model's namespacing habit.
    _NAME_PREFIXES = ("functions/", "functions.", "tools/", "tools.")

    @classmethod
    def canonical_tool_name(cls, name: str) -> str:
        """Strip a model-added namespace prefix. Idempotent, and safe on a plain name."""
        cleaned = (name or "").strip()
        for prefix in cls._NAME_PREFIXES:
            if cleaned.lower().startswith(prefix):
                return cleaned[len(prefix):]
        return cleaned

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            pass
        canonical = self.canonical_tool_name(name)
        try:
            return self._tools[canonical]
        except KeyError as exc:
            # Name the tools we DO have. "unknown tool: functions/read_logs" with no list sent
            # the operator to the source to find out what was available.
            available = ", ".join(self.names()[:40])
            raise ToolError(
                f"unknown tool: {name!r}"
                + (f" (also tried {canonical!r})" if canonical != name else "")
                + f". Available: {available}"
            ) from exc

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, *, kind: Optional[ToolKind] = None) -> list[ToolSpec]:
        items = [self._tools[n] for n in self.names()]
        return [s for s in items if kind is None or s.kind == kind]

    # ---- schema emission --------------------------------------------------
    def openai_schema(self, *, kind: Optional[ToolKind] = None) -> list[dict]:
        return [s.openai() for s in self.specs(kind=kind)]

    def mcp_schema(self, *, kind: Optional[ToolKind] = None) -> list[dict]:
        return [s.mcp() for s in self.specs(kind=kind)]

    # ---- invocation -------------------------------------------------------
    def invoke(
        self,
        name: str,
        ctx: Any,
        *,
        project_id: str,
        actor: str,
        args: dict,
        operator_confirmed: bool = False,
        auth: Any = None,
        internal: Optional[dict] = None,
    ) -> dict:
        """`args` are the CALLER's (a model, an MCP client, the CLI, a pressed offer): only
        the keys the tool's schema lists reach the handler (`schema_args`). `internal` is BOSS's
        own context for the call (an agent turn's `_thread_id`, `_role`, `_writes_allowed`):
        underscore keys only, merged after the filter, never from the caller."""
        spec = self.get(name)
        if (
            spec.kind == "write" and spec.requires_durable_workspace
            and not getattr(ctx, "durable_workspace_configured", True)
        ):
            return {
                "ok": False,
                "tool": name,
                "status": "blocked",
                "error": "durable_workspace_required",
                "message": (
                    "This BOSS command creates durable work, receipts, approvals, and leases. "
                    "Configure DATABASE_URL for self-hosted Postgres and run "
                    "`boss-engine bootstrap` before invoking write tools."
                ),
            }
        # THE ACK, ENFORCED AT THE DOOR RATHER THAN PER TOOL.
        #
        # The agent loop already refuses to press these itself. The model, MCP, and CLI can
        # supply args, so confirmation must arrive through a separate trusted call path.
        #
        # `run_on_host` has always checked this for itself; the check belongs to the CLASS, not
        # to whichever tool remembered.
        # ONLY WHAT THE SCHEMA LISTS (PR #161 review D1). A handler reading a key its schema
        # does not show was a way for any caller to set what the tool never offered:
        # `create_code_task(person_request=True, replaces=[...])` from an MCP client removed
        # contract items. Every tool, one rule: unknown keys are dropped (and named in the
        # result), and BOSS's own context arrives through `internal`, never through `args`.
        safe_args, ignored = schema_args(spec, args)
        safe_args.update({k: v for k, v in (internal or {}).items() if str(k).startswith("_")})
        safe_args.pop("_operator_confirmed", None)
        # ONE ACTING PROJECT FOR THE GATE AND THE HANDLER (H1). The gate used to read
        # `args.project_id` while a pinned handler dropped it and acted in the session project, so
        # a caller holding a permission in project B could act in project A by naming B. Decided
        # here, for every tool: a pinned registry acts in the session project and the argument is
        # discarded; otherwise an explicit `project_id` argument IS the acting project, and the
        # handler is handed that same project (a handler that ignores the argument can no longer
        # act somewhere the gate did not check).
        project_id = acting_project(spec, project_id, safe_args)
        if operator_confirmed:
            safe_args["_operator_confirmed"] = True
        # ANOTHER PROJECT, NAMED ON PURPOSE. A pinned registry discards `project_id`, but takes
        # the one generic `project` argument (PROJECT_ARG): the caller's id or slug for another
        # project in their organization. It is resolved here, for every tool, and the call acts
        # there only if the caller holds the tool's permission IN THAT PROJECT (decided whatever
        # the authz mode — a cross-project call is never passed through on trust). The offer
        # below keeps the argument, so a button pressed later acts where it said it would.
        effective_auth = auth if auth is not None else CURRENT_TOOL_AUTH.get()
        named_project = ""
        if getattr(spec, "pinned_project", False):
            named_project = str(safe_args.get(PROJECT_ARG) or "").strip()
        if not spec.self_executable and not operator_confirmed:
            return {
                "ok": False, "tool": name, "status": "confirmation_required",
                # `requires_confirmation` and `do_this` are the shape `run_on_host` has always
                # refused with, and its refusal was better than a bare error: it tells the model
                # what to do next instead of leaving it to guess. Every tool in this class now
                # refuses that well, rather than only the one that remembered to.
                "requires_confirmation": True,
                "error": "operator_confirmation_required",
                "reversibility": spec.reversibility,
                "do_this": (
                    f"call propose_action with tool='{name}' and these exact args, plus a label "
                    "saying what it does and why. The console renders it as a button; nothing "
                    "runs until the operator clicks it."
                ),
                # VERBATIM. A confirmation button is worthless if what runs is not what was
                # shown, so the arguments come back untouched for the proposal to carry.
                "proposed_args": safe_args,
                "message": (
                    f"'{name}' is declared '{spec.reversibility}': it {offer_warning(spec)}, "
                    "so it runs only when an operator clicks it."
                ),
            }
        if getattr(spec, "pinned_project", False):
            safe_args.pop(PROJECT_ARG, None)
        if named_project:
            target, refusal = resolve_named_project(ctx, named_project, effective_auth)
            if refusal is not None:
                return {"tool": name, **refusal}
            if target != project_id:
                refusal = cross_project_refusal(spec, effective_auth, target, safe_args)
                if refusal is not None:
                    return refusal
                project_id = target
        # IDENTITY (IDENTITY.md §6.4): the caller must hold the tool's permission at the
        # session's project before the handler runs. An agent run is checked against its run
        # token, so a non-delegable permission is always refused to an agent.
        refused = identity_gate(spec, effective_auth, project_id, safe_args)
        if refused is not None:
            return refused
        # A REQUIRED ARGUMENT THE CALL LEFT OUT IS REFUSED, NEVER RUN EMPTY. find_tools called
        # with `{}` searched for nothing, returned no tools, and the agent reported that no
        # deploy tool existed (2026-11-20). Every tool, one rule, checked after
        # every gate (a refusal there says more) and before the handler: the model is told which
        # argument is missing and what the tool takes, so it can call again.
        # AN ARGUMENT THE TOOL DOES NOT TAKE IS REFUSED, NEVER DROPPED INTO A SUCCESS
        # (BOSSAPP-1361). update_deployment_target was called with `health_check_path`, dropped
        # it, found "nothing to change" and reported SUCCEEDED — the health check stayed wrong
        # and the run said it was fixed. Nothing runs; the model is told what the tool takes and
        # calls again. After every gate, like a missing argument (a refusal there says more).
        # Dropped quietly as before: BOSS's own underscore keys (never a caller's to send) and a
        # claimed `actor` (the acting identity is the session's, whatever a caller claims).
        unknown = unknown_caller_args(ignored)
        if unknown:
            return unknown_args_result(spec, unknown)
        missing = missing_required(spec, safe_args)
        if missing:
            return missing_args_result(spec, missing, ignored)
        # The handler sees the identity the gate decided: an explicit `auth` is this call's
        # caller for the handler too (one that records who acted reads CURRENT_TOOL_AUTH).
        auth_token = CURRENT_TOOL_AUTH.set(auth) if auth is not None else None
        # Nothing a caller invokes runs with a step's trusted values (a workflow build step's
        # person_request / replaces): those reach a handler only from the runner's own call.
        trusted_token = TRUSTED_STEP_ARGS.set({})
        try:
            result = spec.handler(ctx, project_id=project_id, actor=actor, args=safe_args)
        except ToolError:
            raise
        except Exception as exc:  # normalize handler failures into a tool result
            consent = consent_result(name, safe_args, exc)
            if consent is not None:
                return consent
            # A refusal that knows its own shape says it (BranchOwned: the run it waits on), so
            # a caller acts on the kind, never on the sentence.
            said = getattr(exc, "tool_result", None)
            err_msg = f"{type(exc).__name__}: {exc}"
            from ..mcp.server import (
                format_database_error,
                is_database_error,
                resolve_database_config,
            )

            if is_database_error(exc):
                settings_obj = getattr(ctx, "settings", None)
                db_url = str(getattr(settings_obj, "database_url", "") or "")
                db_source = str(getattr(ctx, "database_source", "") or "")
                if not db_source:
                    cfg = resolve_database_config()
                    db_source = cfg.source
                    if not db_url:
                        db_url = cfg.url
                err_msg = format_database_error(exc, database_url=db_url, source=db_source)
            return {**(said if isinstance(said, dict) else {}),
                    "ok": False, "tool": name, "error": err_msg}
        finally:
            TRUSTED_STEP_ARGS.reset(trusted_token)
            if auth_token is not None:
                CURRENT_TOOL_AUTH.reset(auth_token)
        if not isinstance(result, dict):
            result = {"result": result}
        result.setdefault("ok", True)
        result.setdefault("tool", name)
        if spec.kind == "write" and result.get("ok") is not False \
                and result.get("status") != "confirmation_required":
            # A WRITE INSIDE A CHAT TURN IS RECORDED ON THE TURN AT ONCE (chat_turns): if the
            # turn fails later, its failure line names this as done rather than "ask again".
            from ..services.chat_turns import note_action

            note_action(name, result)
        if ignored or result.get("ignored_args") == []:
            # A handler that checks its own extra args names what IT dropped; both lists show.
            own = result.get("ignored_args") if isinstance(result.get("ignored_args"), list) else []
            merged = sorted(set(own) | set(ignored))
            if merged:
                result["ignored_args"] = merged
            else:
                result.pop("ignored_args", None)
        return result


def consent_result(name: str, args: dict, exc: BaseException) -> Optional[dict]:
    """A tool refused for want of the person's consent (identity/consent.ConsentRequired), as a
    STRUCTURED result, not an exception's text. Every caller of a tool reads the same shape: the
    agent turn turns it into a "Give consent" offer of this exact call, the press answers it as the
    409 the console's consent sheet opens on, and an MCP client gets the console link to grant at.
    It was `"ConsentRequired: 409: {...}"`, which the agent printed as a raw refusal."""
    from ..identity.consent import ConsentRequired

    if not isinstance(exc, ConsentRequired):
        return None
    body = dict(getattr(exc, "body", None) or {})
    agents = [str(a.get("name") or a.get("id")) for a in body.get("agents") or [] if isinstance(a, dict)]
    who = ", ".join(agents) or "The agents"
    where = body.get("grant_url") or body.get("grant_href")
    return {
        "ok": False, "tool": name, "error": "consent_required",
        "consent": body,
        "consent_id": body.get("consent_id"),
        "grant_href": body.get("grant_href"),
        **({"grant_url": body["grant_url"]} if body.get("grant_url") else {}),
        "args": {k: v for k, v in (args or {}).items() if not str(k).startswith("_")},
        "message": (
            f"{who} need your consent to act for you on {body.get('task') or 'this'}. Nothing ran. "
            + (f"Give it at {where}, then run the same call again." if where
               else "Give it in the console (My account > Agents acting for me), then run the same call again.")),
    }


def acting_project(spec: "ToolSpec", session_project: Optional[str], args: dict) -> Optional[str]:
    """The project a tool call acts in, normalising `args` in place to agree with it (H1).

    Pinned specs: the session project; any `project_id` argument is removed. Others: the
    `project_id` argument when present (then written back so the handler sees the same value),
    else the session project."""
    if getattr(spec, "pinned_project", False):
        args.pop("project_id", None)
        return session_project
    supplied = str(args.get("project_id") or "").strip()
    if supplied:
        args["project_id"] = supplied
        return supplied
    args.pop("project_id", None)
    return session_project


#: THE ONE ARGUMENT THAT POINTS A PINNED TOOL AT ANOTHER PROJECT. Every project-scoped tool in a
#: pinned registry carries it (tools/builtin.build_discuss_read_registry adds it from the spec, so
#: a tool added tomorrow has it too), and `invoke` resolves and authorizes it. Asked about three
#: projects from a conversation in a fourth, the agent said it "has no tool that lets one
#: conversation act on a different project's settings or repositories" — true, and the cause of
#: every guess that followed.
PROJECT_ARG = "project"

PROJECT_ARG_SCHEMA: dict = {
    "type": "string",
    # Short on purpose: it is repeated on every project tool the model is shown.
    "description": "other project id/slug",
}


def resolve_named_project(ctx: Any, ref: str, auth: Any) -> tuple[Optional[str], Optional[dict]]:
    """(project id, None) for a project id or slug in the caller's organization, else
    (None, the refusal). A project that does not exist is a FACT to report, not an error to
    retry: the refusal names it and the projects the organization does have."""
    ref = str(ref or "").strip()
    begin = getattr(ctx, "begin", None)
    if not callable(begin) or getattr(ctx, "engine", True) is None:
        return None, {"ok": False, "error": "project_lookup_unavailable",
                      "message": f"Cannot look up project {ref!r} here: no database."}
    org = str(getattr(getattr(auth, "principal", None), "org_id", "") or "")
    if not org:
        org = str(getattr(getattr(ctx, "settings", None), "default_org_id", "") or "")
    from sqlalchemy import text as _sql

    with begin() as c:
        rows = c.execute(_sql(
            "SELECT p.id, p.slug FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
            " WHERE (:org = '' OR w.org_id = :org) "
            "   AND (p.id = :r OR lower(p.slug) = lower(:r)) "
            " ORDER BY (p.id = :r) DESC LIMIT 1"), {"r": ref, "org": org}).mappings().all()
        if rows:
            return str(rows[0]["id"]), None
        known = [str(r[0]) for r in c.execute(_sql(
            "SELECT p.slug FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
            " WHERE (:org = '' OR w.org_id = :org) ORDER BY p.slug LIMIT 50"), {"org": org})]
    return None, {"ok": False, "error": "project_not_found", "project": ref,
                  "message": f"There is no project {ref!r} in this organization.",
                  "projects_in_this_organization": known}


def cross_project_refusal(spec: "ToolSpec", auth: Any, project_id: str,
                          args: Optional[dict] = None) -> Optional[dict]:
    """A call pointed at another project with PROJECT_ARG: refused unless a verified caller holds
    the tool's permission in that project. Decided in every authz mode; `identity_gate` then
    decides the call as usual at that project."""
    from ..identity import authz
    from ..identity.scope import ResolvedScope

    name = getattr(spec, "name", "?")
    permission_for = getattr(spec, "permission_for", None)
    permission = str((permission_for(args) if permission_for else spec.permission)
                     or "project:read")
    if auth is None:
        return {"ok": False, "tool": name, "error": "identity_unavailable",
                "message": (f"'{name}' in project {project_id}: acting in another project needs "
                            "a verified caller, and this call has none.")}
    try:
        decision = authz.check(auth, permission,
                               ResolvedScope.in_project(auth.principal.org_id, project_id))
        allowed = bool(decision.allowed)
    except Exception:  # noqa: BLE001 — a decision that cannot be made is not an allow
        allowed = False
    if allowed:
        return None
    return {"ok": False, "tool": name, "error": "forbidden", "permission": permission,
            "project": project_id,
            "message": (permission_refusal_words(name, permission)
                        + f" (in project {project_id})")}


#: The verified identity of whoever is calling tools on this thread (an AuthContext). Set by the
#: transport that authenticated the caller (the MCP server, the API's agent endpoints, the run
#: supervisor with a run token); `invoke(auth=...)` overrides it for one call. Never set from a
#: tool argument.
CURRENT_TOOL_AUTH: ContextVar[Any] = ContextVar("boss_tool_auth", default=None)

#: Keys every tool accepts whatever its schema: the acting project (acting_project) and the
#: named other project (PROJECT_ARG), both decided by `invoke` itself.
TRANSPORT_ARGS = frozenset({"project_id", PROJECT_ARG})


def missing_required(spec: "ToolSpec", args: dict) -> list[str]:
    """The schema's `required` arguments `args` does not carry: absent, null, or (for a string
    argument) blank. Blank text is no query, no path, no name. `project_id` and the named-
    project argument are never missing: the call acts in the session's project without them
    (`acting_project`)."""
    schema = spec.parameters or {}
    props = schema.get("properties") or {}
    out = []
    for key in schema.get("required") or []:
        if key in ("project_id", PROJECT_ARG):
            continue
        value = args.get(key)
        declared = (props.get(key) or {}).get("type")
        is_string = declared == "string" or (isinstance(declared, list) and "string" in declared)
        if value is None or (is_string and isinstance(value, str) and not value.strip()):
            out.append(str(key))
    return out


def missing_args_result(spec: "ToolSpec", missing: list[str], ignored: list[str]) -> dict:
    """The refusal for a call missing required arguments: what is missing, what the tool
    takes, and that the call can be made again — a result the model reads and acts on."""
    props = (spec.parameters or {}).get("properties") or {}
    takes = {k: str((v or {}).get("description") or (v or {}).get("type") or "")
             for k, v in props.items()}
    example = json.dumps({"name": spec.name, "arguments": {m: "..." for m in missing}})
    out = {
        "ok": False, "tool": spec.name, "error": "missing_required_arguments",
        "missing": list(missing), "takes": takes,
        "message": (f"'{spec.name}' was called without its required argument(s) "
                    + ", ".join(repr(m) for m in missing)
                    + ", so nothing ran. Call it again with them inside \"arguments\", e.g. "
                    + example + "."),
    }
    if ignored:
        out["ignored_args"] = list(ignored)
    return out


#: Caller keys a tool that does not list them drops without refusing the call (see `invoke`).
QUIET_ARGS = frozenset({"actor"})


def unknown_caller_args(ignored: list[str]) -> list[str]:
    """Of the keys `schema_args` dropped, the ones a caller sent that the tool does not take —
    refused at every door (registry.invoke, the agent-step tool bundles), never dropped into a
    success. Underscore keys and QUIET_ARGS are not a caller's to send and stay dropped."""
    return [k for k in ignored if not str(k).startswith("_") and k not in QUIET_ARGS]


def unknown_args_result(spec: "ToolSpec", unknown: list[str]) -> dict:
    """The refusal for a call naming arguments the tool does not take: which ones, what it does
    take, and that nothing ran — a result the model reads and calls again from."""
    props = (spec.parameters or {}).get("properties") or {}
    takes = {k: str((v or {}).get("description") or (v or {}).get("type") or "")
             for k, v in props.items()}
    return {
        "ok": False, "tool": spec.name, "error": "unknown_arguments",
        "unknown": list(unknown), "ignored_args": list(unknown), "takes": takes,
        "message": (f"'{spec.name}' does not take " + ", ".join(repr(u) for u in unknown)
                    + ", so nothing ran. Call it again with only the arguments it takes: "
                    + (", ".join(sorted(takes)) or "none") + "."),
    }


def schema_args(spec: "ToolSpec", args: Optional[dict]) -> tuple[dict, list[str]]:
    """(the caller's args the tool's schema lists, the keys dropped). Underscore keys are
    BOSS's (an agent turn's ambient context, the operator's confirmation) and never come from
    a caller: `invoke` takes them from `internal`."""
    schema = spec.parameters or {}
    # A schema that declares no properties at all is OPEN (JSON Schema: any key): it filters
    # nothing but BOSS's own underscore keys.
    open_schema = (("properties" not in schema and schema.get("additionalProperties") is not False)
                   or bool(getattr(spec, "handler_checks_extra_args", False)))
    props = set((schema.get("properties") or {}).keys())
    kept, ignored = {}, []
    for key, value in dict(args or {}).items():
        if key in props or key in TRANSPORT_ARGS or (open_schema
                                                    and not str(key).startswith("_")):
            kept[key] = value
        elif key != "_operator_confirmed":
            ignored.append(str(key))
    return kept, sorted(ignored)


#: A workflow STEP's trusted values for the tool it calls (workflow_capability_runner.
#: _invoke_tool sets them, under the step's recorded authority): never a caller's argument.
#: `invoke` clears them for everything it runs, so a model inside a step cannot reach them.
TRUSTED_STEP_ARGS: ContextVar[dict] = ContextVar("boss_trusted_step_args", default={})


def trusted_step_arg(name: str, default: Any = None) -> Any:
    return (TRUSTED_STEP_ARGS.get() or {}).get(name, default)

#: The router key tool calls use for BOSS_AUTHZ_MODE / BOSS_AUTHZ_ROUTER_MODES ("mcp=enforce").
TOOL_ROUTER_KEY = "mcp"


#: The detail of a refusal made because the call carried NO verified identity at all.
NO_IDENTITY_DETAIL = "no verified identity on this tool call"


def not_evaluated_words(tool: str, detail: Any) -> str:
    """What a NOT_EVALUATED refusal says. Never "permission missing": nothing was decided.

    Only a call with truly no verified identity is told its identity is unavailable. Anything
    else that left the permission undecided (the permission check itself raised) is BOSS's own
    failure, said as such, so nobody goes looking for a grant that was never the problem."""
    if str(detail or "") == NO_IDENTITY_DETAIL:
        return (f"Cannot authorize '{tool}': verified identity is unavailable. Restore the "
                "caller's scoped run identity before retrying; this does not establish that a "
                "Passport permission is missing.")
    return (f"Cannot authorize '{tool}': BOSS could not evaluate this permission (an internal "
            "error in BOSS's permission check). This is BOSS's failure, not a missing grant; "
            "nothing needs to be granted.")


def identity_gate(spec: "ToolSpec", auth: Any, project_id: Optional[str],
                  args: Optional[dict] = None) -> Optional[dict]:
    """Decide one tool call (IDENTITY.md §6.4). Returns None to proceed, or the refusal result.

    The project is the one the call ACTS in: the `project_id` argument when the tool takes one
    (handlers use `args.project_id or session project`), else the session's. Every id argument the
    tool declares (object_args.py) must belong to that project (404 otherwise), and a deploying
    tool's production target also needs deployment:production at the target's project.

    Mode follows the API (off | shadow | enforce, per-router key `mcp`): shadow records what it
    would refuse and refuses nothing; enforce refuses, and refuses a call it cannot evaluate (no
    verified identity on this call path) — fail closed. API-surface tools are re-checked by the
    route they call, so they pass here."""
    from ..config import get_settings
    from ..identity import authz
    from ..identity.authz import DenyReason
    from ..identity.enforcement import LEDGER, AuthzMode, Outcome, mode_for
    from ..identity.permissions_gen import PERMISSION_META
    from ..identity.scope import ResolvedScope

    settings = get_settings()
    mode = mode_for(TOOL_ROUTER_KEY, global_mode=settings.authz_mode,
                    router_modes=settings.authz_router_modes)
    if mode == AuthzMode.OFF:
        return None
    permission_for = getattr(spec, "permission_for", None)
    permission = str((permission_for(args) if permission_for else getattr(spec, "permission", None)) or "")
    if getattr(spec, "inherits_route_permission", False):
        # API-surface tools are decided by the route they call — AS THE CALLER (M8: the bridge
        # forwards the caller's own credential, never a server key). A call with no verified
        # identity has no caller to forward, so it is not passed through on trust (H2).
        if auth is not None:
            return None
        outcome = Outcome(False, DenyReason.NOT_EVALUATED, permission or "route",
                          f"tool:{getattr(spec, 'name', '?')}",
                          detail=NO_IDENTITY_DETAIL)
    elif permission not in PERMISSION_META:
        outcome = Outcome(False, DenyReason.UNDECLARED_ROUTE, permission, "undeclared",
                          detail="tool declares no catalogue permission")
    elif auth is None:
        outcome = Outcome(False, DenyReason.NOT_EVALUATED, permission, f"tool:{permission}",
                          detail=NO_IDENTITY_DETAIL)
    else:
        meta = PERMISSION_META[permission]
        org = auth.principal.org_id
        # `invoke` already normalised the acting project (acting_project); a direct caller of
        # the gate gets the same rule.
        acting_in = acting_project(spec, project_id, dict(args or {})) or None
        org_wide = bool(getattr(spec, "acts_org_wide", lambda _a: False)(args))
        scope = (ResolvedScope.org_level(org)
                 if meta.org_only or meta.system_only or org_wide or not acting_in
                 else ResolvedScope.in_project(org, acting_in))
        try:
            decision = authz.check(auth, permission, scope)
            # Every further key the call needs (`also_requires`), each at its own scope; the
            # first refusal is the answer and names its key.
            for extra in (getattr(spec, "also_requires", ()) if decision.allowed else ()):
                extra_meta = PERMISSION_META.get(extra)
                extra_scope = (ResolvedScope.org_level(org)
                               if extra_meta is None or extra_meta.org_only or extra_meta.system_only
                               or org_wide or not acting_in
                               else ResolvedScope.in_project(org, acting_in))
                decision = authz.check(auth, extra, extra_scope)
                if not decision.allowed:
                    permission = extra
                    break
        except Exception as exc:  # noqa: BLE001 — a decision that cannot be made is not an allow
            outcome = Outcome(False, DenyReason.NOT_EVALUATED, permission, f"tool:{permission}",
                              detail=f"authz error: {type(exc).__name__}")
        else:
            if decision.allowed:
                if not (spec.confine or spec.production is not None):
                    return None
                from ..identity.enforcement import evaluate_object_rules
                from ..identity.scope import RequestValues

                rules_at = (ResolvedScope.in_project(org, acting_in) if acting_in
                            else ResolvedScope.org_level(org))
                refused = evaluate_object_rules(
                    spec, auth, rules_at, RequestValues(path_params={}, query={}, body=dict(args or {})),
                    check=lambda c, p, s: authz.check(c, p, s))
                if refused is None:
                    return None
                outcome = refused
            else:
                outcome = Outcome(False, decision.reason, permission, f"tool:{permission}",
                                  auth.principal.kind.value, decision=decision)
    LEDGER.record(router_key=TOOL_ROUTER_KEY, method="TOOL", path_template=str(getattr(spec, "name", "?")),
                  outcome=outcome, mode=mode)
    if mode != AuthzMode.ENFORCE:
        return None
    body = outcome.error_body()
    needs = outcome.permission or permission
    if outcome.reason == DenyReason.NOT_EVALUATED:
        message = not_evaluated_words(getattr(spec, "name", "?"), getattr(outcome, "detail", ""))
    elif outcome.reason == DenyReason.NOT_FOUND:
        message = (f"'{getattr(spec, 'name', '?')}': an id in the arguments names nothing in this "
                   "project.")
    else:
        message = permission_refusal_words(getattr(spec, "name", "?"), needs,
                                           never_delegable=outcome.reason == DenyReason.NOT_DELEGABLE)
    return {"ok": False, "tool": getattr(spec, "name", "?"), "status": body.get("error", "forbidden"),
            "error": body.get("error", "forbidden"), "permission": needs, "message": message}


def extra_permission_refusal(tool: str, permission: str, project_id: Optional[str], *,
                             auth: Any = None) -> Optional[dict]:
    """A handler's SECOND permission for one kind of call, decided exactly as the tool's own
    (`identity_gate`: same mode, same caller, same refusal shape). For a key a call needs only
    sometimes — create_code_task needs code_task:steer only when it builds onto an app
    (`extends_run_id`), which `also_requires` (every call) cannot say. None to proceed."""
    from types import SimpleNamespace

    spec = SimpleNamespace(name=tool, permission=permission, confine=(), production=None)
    return identity_gate(spec, auth if auth is not None else CURRENT_TOOL_AUTH.get(),
                         project_id, {})


def permission_refusal_words(tool: str, permission: Optional[str], *,
                             never_delegable: bool = False) -> str:
    """A refused tool call, said the way a person reads it.

    It read "'start_preview' needs preview:manage." — a tool id and a permission key — and that
    raw text is what a deadlettered workflow showed the operator. Named by the catalogue's own
    title, with the key kept in brackets for whoever has to grant it."""
    action = str(tool or "this step").replace("_", " ").strip() or "this step"
    from ..identity.permissions_gen import PERMISSION_META

    title = ""
    if permission:
        try:
            title = str(PERMISSION_META[permission].title)
        except Exception:  # noqa: BLE001 - an unknown key is still named, just without a title
            title = ""
    if not permission:
        return f"Not allowed to {action}: it declares no permission, so it is refused."
    named = f"\u201c{title}\u201d ({permission})" if title else permission
    if never_delegable:
        return (f"Not allowed to {action}: it needs {named}, which only a person can hold, "
                "never an agent.")
    return (f"Not allowed to {action}: it needs {named}, which whoever is running it does not "
            "hold. An Owner can grant it (for an agent, on its Passport).")


def _empty_object_schema() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}
