"""Coding agents that are a command line, declared rather than coded.

`ClaudeCodeAgent` is 400 lines of process handling written for one binary. Adding Codex or
Cursor that way means writing it again, twice, and the harness registry (BOSS-409) would be a
registry of hand-written special cases — which is the thing it was built to stop being.

So a CLI harness is a SPEC: what to run, how the prompt is delivered, which flag picks the
model, and what to pass when the step is read-only. Adding a provider is a dict entry, and an
operator whose CLI takes different flags can correct them through `BOSS_CLI_HARNESSES_JSON`
without waiting for a release.

What these agents have in common, and why BOSS does not care what they print
---------------------------------------------------------------------------
They all edit files in place in the working directory. So the change of record is the git
diff, and the authoritative pass/fail is BOSS re-running the tests itself — exactly as for
`claude -p`. Their stdout is kept as the summary and is never trusted for anything else,
which is what makes a new one cheap to add: BOSS only has to be able to START it.

Cost
----
CLI agents bill against a subscription and do not report per-call spend the way an API does.
Recorded as zero and flagged, rather than invented: a made-up number in the ledger would
corrupt the one place BOSS answers "what did this cost".
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Optional

_log = logging.getLogger(__name__)

#: Scope under which BOSS's own API stores a subscription credential. One constant so the
#: endpoint that writes it and the harness that reads it cannot disagree about the key.
SUBSCRIPTION_SECRET_SCOPE = "subscription"


@dataclass(frozen=True)
class CliHarnessSpec:
    """Everything BOSS needs to drive one coding CLI."""

    name: str
    #: The executable. Looked up on PATH; a missing binary is a clear error, not a crash.
    binary: str
    #: Argv before the prompt. `{model}` is substituted when a model is supplied.
    args: tuple[str, ...] = ()
    #: Extra argv for a WRITE step (implement/repair).
    write_args: tuple[str, ...] = ()
    #: Extra argv for a READ-ONLY step (review/security). Empty means the harness has no
    #: read-only mode, and BOSS refuses to use it for one rather than letting a reviewer edit.
    read_only_args: tuple[str, ...] = ()
    #: Flag that writes the agent's FINAL message to a file, e.g. "--output-last-message".
    #: Worth using where it exists: the review gate parses FINDING/VERDICT lines out of the
    #: summary, and stdout for these CLIs also carries the diff and a token count, so the
    #: final message on its own is both smaller and less ambiguous.
    final_message_flag: str = ""
    #: Regex with one group capturing total tokens from stdout, for the ledger.
    token_pattern: str = ""
    #: How the prompt reaches the process. argv is simplest; stdin survives a huge prompt.
    prompt_via: str = "argv"
    #: Flag that selects a model, e.g. "--model". Empty means the CLI has no such flag.
    model_flag: str = ""
    #: Seconds before BOSS gives up on a build step. Review steps get review_timeout.
    timeout: int = 2400
    review_timeout: int = 420
    #: Has a governed code task actually completed on this harness?
    verified: bool = False
    description: str = ""
    #: Env the CLI needs to find its own credentials, on top of the agent env allow-list.
    env_passthrough: tuple[str, ...] = ()
    # --- how BOSS discovers this agent on a customer's machine -----------------
    #: Prints the version. Proves the binary runs, not merely that a file exists.
    version_args: tuple[str, ...] = ("--version",)
    #: Asks the CLI whether it is signed in, e.g. ("login", "status").
    auth_check_args: tuple[str, ...] = ()
    #: Matched against that command's output. Empty means the CLI has no login of its own
    #: (it reads an API key from the environment), so being installed is all there is to check.
    auth_ok_pattern: str = ""
    #: Checked FIRST, and it wins. A positive pattern is almost always a substring of the
    #: sentence that denies it — `codex login status` answers "Not logged in", which matches
    #: `logged\s+in` perfectly. Measured on the deployment: a signed-out Codex reported
    #: `authenticated: true, auth_detail: "Not logged in", ready: true`, which would have sent
    #: real work to a CLI that could not do it and made the failure look like the model's.
    #: The default covers how these CLIs actually phrase a denial, so a new spec inherits the
    #: guard rather than the trap.
    auth_fail_pattern: str = (
        r"(?i)\bnot\s+(?:logged\s+in|signed\s+in|authenticated|authorized|authorised)\b"
        r"|\blogged\s+out\b|\bplease\s+(?:log|sign)\s*in\b")
    #: What to tell the operator to run when it is installed but not signed in. This is the
    #: whole value of detection: a specific next command instead of "not configured".
    signin_hint: str = ""
    #: How to install it. Shown ALWAYS, not only when missing — somebody re-doing a login, or
    #: moving to a new machine, needs the same two commands as somebody starting, and a UI
    #: that hides them once it is happy makes the second visit worse than the first.
    install_hint: str = ""
    #: Where the operator can read the CLI's own docs, for the cases neither command covers.
    docs_url: str = ""
    # --- how a subscription reaches a SERVER -----------------------------------
    #
    # Detection finds a CLI on the machine BOSS is running on. That is the whole answer for a
    # self-hosted install, and no answer at all for a deployed one: the operator's login is on
    # their laptop and the pipeline runs in a container. Measured — the deployed API reported
    # `codex installed=False` while the same check on the laptop said ready.
    #
    # So a subscription that can be used remotely is one whose CLI accepts a token: claude-code
    # reads CLAUDE_CODE_OAUTH_TOKEN, codex takes `codex login --with-access-token` on stdin.
    # A CLI with neither can only ever be used where its login lives.
    token_env: str = ""
    #: argv that authorises the CLI from that token, read on stdin.
    token_login_args: tuple[str, ...] = ()
    # --- the credential FILE route ---------------------------------------------
    #
    # Measured against codex-cli 0.154.0, because the token route above does NOT work for a
    # ChatGPT subscription and the documentation does not say so. Every field the CLI's own
    # auth file holds was rejected by `codex login --with-access-token`:
    #
    #   access_token   -> agent identity JWT payload is not valid JSON
    #   id_token       -> agent identity JWT payload is not valid JSON
    #   refresh_token  -> agent identity JWT payload is not valid base64url
    #
    # The token is a perfectly good JWT with proper OAuth claims; that flag simply wants a
    # different credential type — an agent identity JWT — which a consumer plan never mints.
    # Worse, merely having CODEX_ACCESS_TOKEN in the environment makes `codex login status`
    # ERROR rather than report a status, so the token route does not degrade, it breaks.
    #
    # What does work, verified: place the CLI's own auth file in its home and it reports
    # "Logged in using ChatGPT". So the thing that travels is the FILE, not a field from it.
    #: Secret key (and env var) holding the CONTENTS of the CLI's credential file.
    auth_file_secret: str = ""
    #: Env var by which the CLI is told where its home directory is.
    home_env: str = ""
    #: Where that home is when the env var is unset.
    default_home: str = ""
    #: The credential file's name inside that home.
    auth_file_name: str = ""
    #: How the operator mints the travelling token on their own machine.
    token_hint: str = ""
    #: The CLI's own credential file on a signed-in machine, when minting the travelling token
    #: means reading one. Set only where the CLI has no `setup-token` equivalent: claude-code
    #: prints a token on demand, codex does not — so for codex the file IS the route, and the
    #: operator would otherwise be told to open it and copy a live credential by hand.
    #: Named here so the helper script, the console and the docs cannot disagree about it.
    token_source_file: str = ""
    #: Field to read out of that file. Searched at any depth, since these files nest.
    token_source_field: str = "access_token"
    #: The entitlement source this CLI satisfies, so connecting it records governance state
    #: rather than only flipping a setting.
    entitlement_source_id: str = ""
    #: Provider the entitlement belongs to.
    entitlement_provider: str = ""

    def supports_read_only(self) -> bool:
        return bool(self.read_only_args)


#: The CLIs BOSS ships a spec for. NONE is marked verified: a spec written from a CLI's
#: documentation is a hypothesis until a governed task completes on it, and presenting an
#: untested adapter as ready is how a registry stops meaning anything.
BUILTIN_CLI_HARNESSES: dict[str, CliHarnessSpec] = {
    # VERIFIED AGAINST codex-cli 0.154.0, not taken from documentation. `codex exec` is the
    # non-interactive mode; `--sandbox` is a real flag with a real read-only value, which is
    # what lets Codex hold "the reviewer cannot edit". Smoke-tested: edits a file in place in
    # ~22s on a ChatGPT-subscription login with no API key.
    "codex": CliHarnessSpec(
        name="codex",
        binary="codex",
        args=("exec", "--skip-git-repo-check", "--color", "never"),
        write_args=("--sandbox", "workspace-write"),
        read_only_args=("--sandbox", "read-only"),
        prompt_via="argv",
        model_flag="--model",
        final_message_flag="--output-last-message",
        token_pattern=r"tokens used\s+([\d,]+)",
        verified=False,
        description="OpenAI Codex CLI (`codex exec`). Authorised by the Codex CLI's own "
                    "login — a ChatGPT subscription, not an API key.",
        env_passthrough=("CODEX_HOME", "OPENAI_API_KEY"),
        version_args=("--version",),
        auth_check_args=("login", "status"),
        auth_ok_pattern=r"(?i)logged\s+in",
        signin_hint="codex login",
        install_hint="npm install -g @openai/codex",
        # NOT token_env. `--with-access-token` rejects every field a ChatGPT subscription
        # holds, and setting CODEX_ACCESS_TOKEN makes `codex login status` error outright —
        # see the note on `auth_file_secret`. The file is the route that works.
        auth_file_secret="CODEX_AUTH_JSON",
        home_env="CODEX_HOME",
        default_home="~/.codex",
        auth_file_name="auth.json",
        token_hint="on a machine where you are signed in, run "
                   "scripts/store_subscription_token.py codex — it stores the Codex CLI's own "
                   "credential file so nobody has to handle a credential by hand",
        token_source_file="~/.codex/auth.json",
        docs_url="https://developers.openai.com/codex/cli",
        entitlement_source_id="openai_codex_chatgpt",
        entitlement_provider="openai",
    ),
    "cursor": CliHarnessSpec(
        name="cursor",
        binary="cursor-agent",
        args=("-p",),
        write_args=(),
        read_only_args=(),
        prompt_via="argv",
        model_flag="--model",
        verified=False,
        description="Cursor CLI agent in print mode. Authorised by Cursor's own login.",
        env_passthrough=("CURSOR_API_KEY",),
        version_args=("--version",),
        # UNVERIFIED: taken from Cursor's docs, not from the binary. Correct it with
        # BOSS_CLI_HARNESSES_JSON if the real subcommand differs.
        auth_check_args=("status",),
        auth_ok_pattern=r"(?i)logged\s+in|authenticated",
        signin_hint="cursor-agent login",
        install_hint="curl https://cursor.com/install -fsS | bash",
        docs_url="https://docs.cursor.com/en/cli/overview",
        entitlement_source_id="cursor_subscription",
        entitlement_provider="cursor",
    ),
    "aider": CliHarnessSpec(
        name="aider",
        binary="aider",
        args=("--yes-always", "--no-auto-commit", "--message"),
        write_args=(),
        read_only_args=(),
        prompt_via="argv",
        model_flag="--model",
        verified=False,
        description="Aider, non-interactive single message.",
        env_passthrough=("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"),
        version_args=("--version",),
        # No login of its own: aider reads an API key from the environment, so installed is
        # the whole check and there is nothing to sign in to.
        auth_check_args=(),
        signin_hint="set OPENAI_API_KEY or ANTHROPIC_API_KEY in the environment",
        install_hint="python -m pip install aider-install && aider-install",
        docs_url="https://aider.chat/docs/install.html",
    ),
}


def load_specs(settings) -> dict[str, CliHarnessSpec]:
    """Built-in specs, with operator overrides from BOSS_CLI_HARNESSES_JSON merged on top.

    The override exists because these flags are a guess until someone runs them. An operator
    who finds `codex exec` wants a different argument should be able to fix it in an env var
    at 2am, not wait for a release.
    """
    specs = dict(BUILTIN_CLI_HARNESSES)
    raw = (getattr(settings, "cli_harnesses_json", "") or "").strip()
    if not raw:
        return specs
    try:
        overrides = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        _log.warning("BOSS_CLI_HARNESSES_JSON is not valid JSON (%s); using built-in specs",
                     exc)
        return specs
    for name, patch in (overrides or {}).items():
        if not isinstance(patch, dict):
            continue
        base = specs.get(name)
        fields = {f: getattr(base, f) for f in CliHarnessSpec.__dataclass_fields__} if base else {"name": name, "binary": name}
        for key, value in patch.items():
            if key not in CliHarnessSpec.__dataclass_fields__:
                continue
            if key in ("args", "read_only_args", "env_passthrough") and isinstance(value, list):
                value = tuple(value)
            fields[key] = value
        fields["name"] = name
        try:
            specs[name] = CliHarnessSpec(**fields)
        except Exception as exc:  # noqa: BLE001
            _log.warning("ignoring bad CLI harness override %r: %s", name, exc)
    return specs


def _run_probe(binary: str, args: tuple[str, ...], timeout: int = 20) -> tuple[int, str]:
    """Run a short informational command. Never raises: a probe that explodes tells the
    operator less than one that reports what happened."""
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "CI": "1"})
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def detect(spec: CliHarnessSpec) -> dict:
    """Is this coding agent installed on THIS machine, and is it signed in?

    Detection is the difference between a product and a set of instructions. BOSS cannot ask a
    customer to hand over a subscription credential — a Codex or Cursor login is an OAuth
    session held by that CLI, and there is nothing to paste. What BOSS can do is look for the
    CLI the customer already uses, confirm it is signed in, and record the entitlement.

    Never raises. "Not installed" and "installed but signed out" are answers, and each has a
    different next step — which is the whole point of asking.
    """
    import re as _re

    found = shutil.which(spec.binary)
    result = {
        "name": spec.name,
        "binary": spec.binary,
        "installed": bool(found),
        "path": found or None,
        "version": None,
        "authenticated": None,
        "auth_detail": None,
        "ready": False,
        "signin_hint": spec.signin_hint,
        "install_hint": spec.install_hint,
        "docs_url": spec.docs_url,
        "verified_harness": spec.verified,
        "next_step": None,
    }
    # The name of whatever this CLI needs to reach a server: a token for some, the CLI's own
    # credential file for others. One field so a caller does not have to know which route the
    # harness uses in order to say what is missing.
    credential = spec.token_env or spec.auth_file_secret
    result["token_env"] = credential
    result["credential_env"] = credential
    result["credential_kind"] = ("file" if spec.auth_file_secret else
                                 ("token" if spec.token_env else ""))
    result["token_hint"] = spec.token_hint
    #: So a caller can line detection up against what was RECORDED. Without it, "is this
    #: connected" and "is the CLI signed in right now" cannot be shown as the separate
    #: questions they are — and a completed setup reads as an untouched one after a refresh.
    result["entitlement_source_id"] = spec.entitlement_source_id
    result["token_present"] = bool(credential and os.environ.get(credential))
    # CAN THIS SUBSCRIPTION BE USED FROM A SERVER AT ALL? Detection answers "is the CLI here";
    # this answers "could it be, on a machine the operator does not sit at". A CLI with no
    # token route can only ever be used where its login lives, and saying so is the difference
    # between an operator configuring a deployment and one wondering why it stays empty.
    result["portable"] = bool(spec.token_env or spec.auth_file_secret)
    if not found:
        result["next_step"] = (spec.install_hint or f"install the {spec.name} CLI")
        return result
    code, out = _run_probe(found, spec.version_args)
    if code == 0 and out:
        result["version"] = out.splitlines()[0][:120]
    if not spec.auth_check_args:
        # No login of its own — installed is the whole check.
        result["authenticated"] = None
        result["ready"] = True
        result["next_step"] = None
        return result
    code, out = _run_probe(found, spec.auth_check_args)
    # The line that ANSWERS the question, not merely the first one printed. These CLIs emit
    # warnings before their status — a fresh CODEX_HOME produces "WARNING: proceeding, even
    # though we could not create PATH aliases…" — and reporting that as `auth_detail` hands the
    # operator a sentence about symlinks when they are debugging a login. The troubleshooting
    # tables in the setup doc are keyed on this field, so it has to be the status.
    detail = _auth_detail(spec, out)
    result["auth_detail"] = detail or None
    # The denial is checked first and wins outright: "Not logged in" contains "logged in", so
    # a positive-only test reports a signed-out CLI as ready.
    denied = bool(spec.auth_fail_pattern) and bool(_re.search(spec.auth_fail_pattern, out or ""))
    signed_in = (not denied and bool(spec.auth_ok_pattern)
                 and bool(_re.search(spec.auth_ok_pattern, out or "")))
    result["authenticated"] = signed_in
    result["ready"] = signed_in
    if not signed_in:
        # A token already set and still not signed in means the token is the thing to fix,
        # not the browser login — different problem, different command.
        result["next_step"] = (
            f"set {credential} on this install ({spec.token_hint})"
            if credential and not result["token_present"] and not _is_interactive_host()
            else f"sign in with: {spec.signin_hint}")
    return result


#: What a CLI says when its credential was valid and has since been revoked elsewhere. Captured
#: from codex-cli, which reports all three for the same underlying cause — a refresh performed
#: on another machine, which single-uses the token this one holds.
_REVOKED_CREDENTIAL = (
    r"(?i)refresh token was revoked|invalidated oauth token"
    r"|could not be refreshed.*(log ?out|sign in again)")


#: Fields these CLIs use to record when a credential was last issued. Checked in order.
_CREDENTIAL_TIMESTAMPS = ("last_refresh", "last_refreshed", "issued_at", "updated_at")


def _credential_issued_at(content: Optional[str]) -> Optional[str]:
    """When this credential was minted, as the file itself records it."""
    if not content:
        return None
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    for field in _CREDENTIAL_TIMESTAMPS:
        value = parsed.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _newer_credential(on_disk: Optional[str], candidate: Optional[str]) -> Optional[str]:
    """Which of these is more recent — "disk", "candidate", or None when it cannot be told.

    ISO-8601 timestamps compare correctly as strings when both carry the same shape, which
    these do; anything else returns None so the caller falls back to its normal behaviour
    rather than acting on a guess.
    """
    disk_at, candidate_at = _credential_issued_at(on_disk), _credential_issued_at(candidate)
    if not disk_at or not candidate_at or disk_at == candidate_at:
        return None
    return "disk" if disk_at > candidate_at else "candidate"


def _looks_like_revoked_credential(*streams: Optional[str]) -> bool:
    """Did this fail because the credential died, rather than because the work was bad?

    Worth distinguishing: a revoked credential is the one failure BOSS can fix by itself, and
    every other failure is one where retrying would waste a turn of the operator's subscription.
    """
    import re as _re

    blob = "\n".join(s or "" for s in streams)
    return bool(_re.search(_REVOKED_CREDENTIAL, blob))


def _auth_detail(spec: CliHarnessSpec, out: str) -> str:
    """The most informative line of an auth-status output."""
    import re as _re

    lines = [line.strip() for line in (out or "").splitlines() if line.strip()]
    if not lines:
        return ""
    for pattern in (spec.auth_fail_pattern, spec.auth_ok_pattern):
        if not pattern:
            continue
        for line in lines:
            if _re.search(pattern, line):
                return line[:200]
    for line in lines:
        if not _re.match(r"(?i)^\s*(warning|note)\b", line):
            return line[:200]
    return lines[0][:200]


def _is_interactive_host() -> bool:
    """Is BOSS running somewhere a person could open a browser to sign a CLI in?

    A container has no browser and nobody sitting at it, so telling its operator to "run
    codex login" is advice they cannot take. Imperfect by nature — it reads the same signals
    every tool does — and wrong in the safe direction: a false "server" offers the token
    route, which also works on a laptop.
    """
    return not (os.environ.get("CI")
                or os.path.exists("/.dockerenv")
                or os.environ.get("KUBERNETES_SERVICE_HOST"))


def detect_all(settings) -> list[dict]:
    """Every CLI coding agent BOSS knows about, and what each needs to become usable."""
    return [detect(spec) for spec in load_specs(settings).values()]


class CliHarnessUnavailable(RuntimeError):
    """The binary is not installed, or the step needs something the CLI cannot do."""


class GenericCliCodeAgent:
    """Drive any coding CLI that edits files in the working directory."""

    def __init__(self, settings, spec: CliHarnessSpec) -> None:
        self.settings = settings
        self.spec = spec
        self.engine = spec.name

    # -- process ------------------------------------------------------
    def _resolve_binary(self) -> str:
        found = shutil.which(self.spec.binary)
        if not found:
            raise CliHarnessUnavailable(
                f"the {self.spec.name!r} harness needs {self.spec.binary!r} on PATH and it is "
                f"not installed. Install the CLI and sign it in, or choose another harness "
                f"(BOSS_CODE_AGENT_ENGINE / BOSS_PROVIDER_LOCK)."
            )
        return found

    def _env(self) -> dict:
        from .agent_env import agent_environment

        env = agent_environment(os.environ)
        # A CLI signed in with its own OAuth needs its own home/config to find the session.
        for name in self.spec.env_passthrough:
            value = os.environ.get(name)
            if value:
                env[name] = value
        env.setdefault("HOME", os.environ.get("HOME", ""))
        env["CI"] = "1"
        return env

    def _argv(self, prompt: str, *, read_only: bool, model: Optional[str]) -> list[str]:
        argv = [self._resolve_binary(), *self.spec.args]
        # Mode-specific argv rather than one list: codex takes `--sandbox read-only` vs
        # `--sandbox workspace-write` on the SAME flag, so appending a read-only suffix to a
        # list that already carried the write value would leave two copies and let argument
        # order decide whether a reviewer can edit the code it is reviewing.
        argv += list(self.spec.read_only_args if read_only else self.spec.write_args)
        if model and self.spec.model_flag:
            argv += [self.spec.model_flag, model]
        if self.spec.prompt_via == "argv":
            argv.append(prompt)
        return argv

    def ensure_authorised(self) -> dict:
        """Sign the CLI in from a stored token, when it is not signed in already.

        A subscription is authorised on the operator's laptop; the pipeline runs in a
        container. The bridge is the token the CLI itself mints for headless use — which is
        how claude_code has always worked here (CLAUDE_CODE_OAUTH_TOKEN), and which codex
        accepts on stdin via `codex login --with-access-token`.

        The token is read from the deployment's secret store, never from a file this code
        writes and never from an argument: argv is visible in `ps` to every process on the
        box, so it is piped in. Nothing about the value is logged, including its length.
        """
        has_file_route = bool(self.spec.auth_file_secret and self.spec.auth_file_name)
        if not has_file_route and (not self.spec.token_env or not self.spec.token_login_args):
            return {"authorised": None, "reason": "this CLI has no token route"}
        found = detect(self.spec)
        if not found["installed"]:
            return {"authorised": False, "reason": "not installed"}
        if has_file_route:
            # DELIBERATELY BEFORE the "already signed in" shortcut. A credential file left in
            # place by an earlier run makes the CLI report signed in, so short-circuiting on
            # that meant a NEWER credential in the store was never written — and the operator
            # who re-sent one, exactly as told, saw nothing change. Measured: the CLI said
            # "Logged in using ChatGPT" while every turn failed 401 on a token the laptop had
            # since rotated. The store is the source of truth; the file is a cache of it.
            return self._authorise_from_file(cli_reports_signed_in=bool(found["authenticated"]))
        if found["authenticated"]:
            return {"authorised": True, "reason": "already signed in"}
        token = self._resolve_token()
        if not token:
            return {"authorised": False,
                    "reason": f"no {self.spec.token_env} available to this install"}
        try:
            proc = subprocess.run(
                [self._resolve_binary(), *self.spec.token_login_args],
                input=token, capture_output=True, text=True, timeout=60,
                env=self._env(),
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return {"authorised": False, "reason": f"{type(exc).__name__}"}
        if proc.returncode != 0:
            # The CLI's own message can echo the token back; only the status is safe to keep.
            _log.warning("%s token login failed with exit %s", self.spec.name, proc.returncode)
            return {"authorised": False, "reason": f"token rejected (exit {proc.returncode})"}
        _log.info("%s authorised from %s", self.spec.name, self.spec.token_env)
        return {"authorised": True, "reason": "signed in from the stored token"}

    def cli_home(self) -> str:
        """Where this CLI keeps its own state, honouring its home env var when set."""
        configured = os.environ.get(self.spec.home_env, "") if self.spec.home_env else ""
        return os.path.expanduser(configured or self.spec.default_home or "~")

    def _write_auth_file(self, content: str) -> str:
        """Place the credential file with an owner-only mode, atomically.

        Created 0600 BEFORE anything is written to it — opening 0644 and chmod-ing afterwards
        leaves a window in which any process on the box can read a live session. The rename is
        atomic so a concurrent run never observes a half-written credential.
        """
        home = self.cli_home()
        os.makedirs(home, mode=0o700, exist_ok=True)
        final = os.path.join(home, self.spec.auth_file_name)
        tmp = f"{final}.boss-tmp"
        handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, final)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return final

    def _current_auth_file(self) -> Optional[str]:
        """What the CLI is using right now, if anything."""
        path = os.path.join(self.cli_home(), self.spec.auth_file_name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return None

    def _authorise_from_file(self, cli_reports_signed_in: bool = False) -> dict:
        """Sign the CLI in by restoring its own credential file from the secret store.

        The stored credential WINS over whatever is on disk, because the file is only ever a
        copy of it and a copy goes stale: the machine that minted it rotates its token, which
        invalidates every other copy, and the CLI goes on reporting "logged in" from a file
        whose token now 401s. Trusting the file meant re-sending a fresh credential changed
        nothing at all.

        With nothing stored, the file is all there is — that is an install where the operator
        signed the CLI in themselves, and overwriting it would be taking away the only
        credential they have.
        """
        content = self._resolve_secret(self.spec.auth_file_secret)
        if not content:
            if cli_reports_signed_in:
                return {"authorised": True, "reason": "signed in with the CLI's own session"}
            return {"authorised": False,
                    "reason": f"no {self.spec.auth_file_secret} available to this install"}
        on_disk = self._current_auth_file()
        if cli_reports_signed_in and (on_disk or "").strip() == content.strip():
            # Already running the stored credential; rewriting it would prove nothing.
            return {"authorised": True, "reason": "already signed in from the stored credential"}
        # NEVER GO BACKWARDS. These credentials carry the moment they were issued, and an older
        # one is not merely redundant — it is revoked, because every refresh invalidates what
        # came before. A three-day-old value left in an environment variable by an earlier
        # experiment was restored over a session created minutes before, on every attempt, and
        # the CLI then failed 401 on a credential BOSS had just put there itself.
        newer = _newer_credential(on_disk, content)
        if newer == "disk" and cli_reports_signed_in:
            _log.info("%s already holds a newer credential than the stored one; keeping it",
                      self.spec.name)
            return {"authorised": True,
                    "reason": "the CLI holds a newer credential than the stored one"}
        if self.spec.auth_file_name.endswith(".json"):
            try:
                json.loads(content)
            except json.JSONDecodeError:
                # Say it is malformed, never what it contained.
                return {"authorised": False,
                        "reason": f"the stored {self.spec.auth_file_secret} is not valid JSON"}
        try:
            self._write_auth_file(content)
        except OSError as exc:
            return {"authorised": False, "reason": f"could not write the credential file: "
                                                   f"{type(exc).__name__}"}
        # ASK THE CLI, do not assume. A restored file can be expired or from another account,
        # and "we wrote a file" is not evidence that anybody is signed in.
        after = detect(self.spec)
        if after["authenticated"]:
            _log.info("%s authorised from the stored %s",
                      self.spec.name, self.spec.auth_file_name)
            return {"authorised": True, "reason": "signed in from the stored credential file"}
        return {"authorised": False,
                "reason": f"the stored credential file did not sign {self.spec.name} in "
                          f"({after.get('auth_detail') or 'no detail'})"}

    def remember_rotated_credential(self) -> bool:
        """Push a credential the CLI has rotated back into the store.

        These CLIs refresh on use, and the refresh REVOKES what came before. So the copy in the
        store is correct for exactly as long as nobody uses it: the first run rotates the pair,
        the file on disk moves on, and the store is left holding a token that is now revoked.
        Nothing notices until the container is rebuilt and BOSS restores that dead credential —
        an install that worked for weeks failing on a routine `up --build`.

        Only when the credential came FROM the store. An operator who sets the environment
        variable themselves owns that value, and writing over it would take a decision that is
        theirs to make.
        """
        if not (self.spec.auth_file_secret and self.spec.auth_file_name):
            return False
        if os.environ.get(self.spec.auth_file_secret):
            return False
        current = self._current_auth_file()
        if not current or not current.strip():
            return False
        try:
            from .secret_backends import backend_for, default_secret_backend
            from .secrets_service import ref_key_for

            store = backend_for(default_secret_backend(self.settings), self.settings)
            key = ref_key_for(SUBSCRIPTION_SECRET_SCOPE, self.spec.auth_file_secret)
            try:
                if (store.get(key) or "").strip() == current.strip():
                    return False        # unchanged; writing would churn the store for nothing
            except Exception:  # noqa: BLE001 — absent is a reason to write, not to stop
                pass
            store.put(key, current.strip())
        except Exception as exc:  # noqa: BLE001 — a run must not fail because a cache is stale
            _log.info("could not write back the rotated %s credential (%s)",
                      self.spec.name, type(exc).__name__)
            return False
        _log.info("%s credential rotated; the stored copy was updated", self.spec.name)
        return True

    def _resolve_secret(self, name: str) -> Optional[str]:
        """A stored value: the environment, then the secret store, under either key.

        Two keys, because there are two ways a credential legitimately arrives. An operator who
        sets it themselves uses the plain name. BOSS's own API stores it through `put_secret`,
        which namespaces keys as BOSS_<SCOPE>_<FIELD> so that a store shared with other systems
        stays readable — deliberate, and it means looking under one name only would miss
        everything the product put there itself.

        The namespaced key is derived, not looked up, so this needs no database connection —
        which matters because it runs inside a sandboxed agent process that must not have one.
        """
        direct = os.environ.get(name)
        if direct:
            return direct.strip() or None
        try:
            from .secret_backends import backend_for, default_secret_backend
            from .secrets_service import ref_key_for

            store = backend_for(default_secret_backend(self.settings), self.settings)
            for key in (name, ref_key_for(SUBSCRIPTION_SECRET_SCOPE, name)):
                try:
                    value = store.get(key)
                except Exception:  # noqa: BLE001 — absent under this key, try the next
                    continue
                if (value or "").strip():
                    return value.strip()
            return None
        except Exception as exc:  # noqa: BLE001 — a missing secret is an answer, not a crash
            _log.info("no stored %s for %s (%s)", name, self.spec.name, type(exc).__name__)
            return None

    def _resolve_token(self) -> Optional[str]:
        """The travelling token: the environment first, then the deployment's secret store.

        Environment first because that is what an operator sets while working something out,
        and a store that silently overrode it would make debugging impossible.
        """
        direct = os.environ.get(self.spec.token_env)
        if direct:
            return direct.strip() or None
        try:
            from .secret_backends import backend_for, default_secret_backend

            # `default_secret_backend` names the best store this installation actually has —
            # a managed one, a vault, or the local fallback — so the harness does not have to
            # know which, and a deployment that changes store keeps working.
            value = backend_for(default_secret_backend(self.settings), self.settings).get(
                self.spec.token_env)
        except Exception as exc:  # noqa: BLE001 — a missing secret is an answer, not a crash
            _log.info("no stored %s for %s (%s)", self.spec.token_env, self.spec.name,
                      type(exc).__name__)
            return None
        return (value or "").strip() or None

    def run_role(self, prompt: str, cwd: Path, *, read_only: bool = False,
                 model: Optional[str] = None, resume_session_id: Optional[str] = None,
                 **_ignored) -> dict:
        from .llm_telemetry import record_llm_call
        from .scheduling import low_priority

        if read_only and not self.spec.supports_read_only():
            # "The reviewer cannot edit" is an invariant of the governed pipeline. A CLI with
            # no read-only mode cannot hold it, so it is refused for the step rather than
            # trusted to behave.
            raise CliHarnessUnavailable(
                f"{self.spec.name!r} has no read-only mode, so it cannot run a review step "
                f"without being able to edit the code it is reviewing. Set "
                f"BOSS_CODE_REVIEW_ENGINE to a harness that can (any API lane), or give this "
                f"one read_only_args via BOSS_CLI_HARNESSES_JSON."
            )
        import re as _re
        import tempfile

        # Sign in from the stored credential if this deployment has one and the CLI is not
        # already signed in — a token for some CLIs, the CLI's own auth file for others.
        # Cheap after the first call: `detect` short-circuits on "already signed in".
        if ((self.spec.token_env or self.spec.auth_file_secret)
                and not getattr(self, "_auth_checked", False)):
            self._auth_checked = True
            self._auth_state = self.ensure_authorised()
        argv = self._argv(prompt, read_only=read_only, model=model)
        final_path = None
        if self.spec.final_message_flag:
            fd, final_path = tempfile.mkstemp(prefix="boss-cli-final-", suffix=".txt")
            os.close(fd)
            # Inserted before the prompt, which `_argv` appended last.
            insert_at = len(argv) - 1 if self.spec.prompt_via == "argv" else len(argv)
            argv[insert_at:insert_at] = [self.spec.final_message_flag, final_path]
        timeout = self.spec.review_timeout if read_only else self.spec.timeout
        started = time.monotonic()
        try:
            proc = subprocess.run(
                low_priority(argv),
                cwd=str(cwd),
                input=(prompt if self.spec.prompt_via == "stdin" else None),
                capture_output=True, text=True, timeout=timeout,
                start_new_session=True, env=self._env(),
            )
        except subprocess.TimeoutExpired:
            record_llm_call(engine_name=self.engine, usage={"model": model or ""},
                            latency_ms=int((time.monotonic() - started) * 1000),
                            ok=False, error=f"timed out after {timeout}s")
            raise CliHarnessUnavailable(
                f"{self.spec.name} did not finish within {timeout}s") from None
        ms = int((time.monotonic() - started) * 1000)
        ok = proc.returncode == 0
        if ok:
            # The CLI may have rotated its credential during that run; keep the store current
            # so a rebuild does not restore a token this run has already invalidated.
            self.remember_rotated_credential()
        elif _looks_like_revoked_credential(proc.stdout, proc.stderr):
            # "Signed in" is not the same as "usable", and only a real turn can tell the
            # difference: the CLI reports a healthy session right up until the moment a rotation
            # elsewhere revokes it. Restoring the stored credential and trying once more is the
            # difference between self-healing and a support ticket.
            restored = self._authorise_from_file(cli_reports_signed_in=False)
            if restored.get("authorised"):
                _log.info("%s credential was revoked; restored and retrying once",
                          self.spec.name)
                try:
                    proc = subprocess.run(
                        low_priority(argv), cwd=str(cwd),
                        input=(prompt if self.spec.prompt_via == "stdin" else None),
                        capture_output=True, text=True, timeout=timeout,
                        start_new_session=True, env=self._env(),
                    )
                    ok = proc.returncode == 0
                    if ok:
                        self.remember_rotated_credential()
                except subprocess.TimeoutExpired:
                    pass
        summary = ""
        if final_path:
            try:
                summary = Path(final_path).read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                summary = ""
            finally:
                try:
                    os.unlink(final_path)
                except OSError:
                    pass
        # Falling back to stdout matters: a CLI that failed before writing its final message
        # still said WHY on stdout, and losing that leaves the operator a bare exit code.
        summary = summary or (proc.stdout or "").strip() or (proc.stderr or "").strip()
        tokens = 0
        if self.spec.token_pattern:
            hit = _re.search(self.spec.token_pattern, proc.stdout or "")
            if hit:
                try:
                    tokens = int(hit.group(1).replace(",", ""))
                except ValueError:
                    tokens = 0
        record_llm_call(
            engine_name=self.engine,
            # Cost is NOT invented: a subscription CLI does not report per-call spend, and a
            # guessed number would corrupt the ledger BOSS answers cost questions from.
            usage={"model": model or self.spec.name, "total_tokens": tokens},
            cost_usd_micros=0, latency_ms=ms, turns=1, ok=ok,
            error=None if ok else (proc.stderr or "")[:400],
        )
        if not ok:
            _log.warning("%s exited %s: %s", self.spec.name, proc.returncode,
                         (proc.stderr or "")[:300])
        return {
            "summary": summary[:8000] or f"({self.spec.name} produced no output)",
            "usage": {"model": model or self.spec.name},
            "cost_usd_micros": 0,
            "cost_unreported": True,
            "turns": 1,
            "model": model or self.spec.name,
            "exit_code": proc.returncode,
        }

    def run(self, spec, repo, sandbox):
        """Whole-task entry point. BOSS re-runs the tests; the diff is the change of record."""
        from .code_agent import CodeAgentResult, load_repo_guide

        if sandbox.path is None:
            raise RuntimeError("Sandbox must be provisioned before running the agent.")
        criteria = "\n".join(f"- {c}" for c in spec.acceptance_criteria) or "- (none)"
        tests = "\n".join(f"- {c}" for c in spec.test_commands) or "- (none)"
        guide = load_repo_guide(sandbox.path) or ""
        prompt = (
            "You are a senior software engineer working in a checked-out git repository.\n"
            f"TASK: {spec.title}\n\n{spec.instructions}\n\n"
            f"ACCEPTANCE CRITERIA:\n{criteria}\n\n"
            f"TEST/BUILD COMMANDS (make these pass):\n{tests}\n"
            + (f"\nREPO GUIDE (the repository's own instructions — follow them):\n{guide}\n"
               if guide else "")
            + "\nEdit files in place. Do NOT create a branch, commit, or push — BOSS handles "
              "git, the pull request and the merge gate.\n"
        )
        out = self.run_role(prompt, sandbox.path)
        results = [sandbox.run(cmd) for cmd in spec.test_commands]
        return CodeAgentResult(
            summary=out["summary"], diff=sandbox.diff(), diff_summary=sandbox.diff_summary(),
            tests=results, tests_passed=all(r.ok for r in results) if results else False,
            cost_usd_micros=0, usage=out["usage"], transcript=out["summary"],
            turns=1, engine=self.engine,
        )
