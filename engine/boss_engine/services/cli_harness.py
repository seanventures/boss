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

import contextlib
import contextvars
from dataclasses import dataclass
import json
import logging
import os
import re
from pathlib import Path
import shutil
import subprocess
import threading
import time
import tomllib
from typing import Optional

from . import model_proxy as _model_proxy
from . import owned_process
from .harness_registry import FailurePattern, RuntimeBinary
from .workspace_perms import tidies_workspace

_log = logging.getLogger(__name__)

# ── What the shipped CLIs exec at runtime (harness_registry.RuntimeBinary) ────────────────────

#: Node, copied into the runtime stage from the official image: a CLI whose npm `bin` is a
#: script (codex.js) runs on it.
NODE_RUNTIME = RuntimeBinary("node", "/usr/local/bin/node", "image",
                             "a script-based npm CLI runs on Node")
#: The `claude` CLI. Its npm package installs the native binary (bin/claude.exe, from a
#: per-platform optional dependency), which embeds ripgrep and needs no Node.
CLAUDE_CLI_RUNTIME = (
    RuntimeBinary("claude", "@anthropic-ai/claude-code", "npm", "the Claude Code CLI"),
)
#: How the Claude CLI says its sandbox did not start (harness_failures). It answers EVERY Bash
#: call with this in place of the command's output, so the agent works blind until it gives up:
#: crun_f103065e carried it on 19 of 206 turns over fifteen minutes of design, implement and
#: repair. The missing binary itself ("… socat not installed") is read generically.
CLAUDE_FAILURE_PATTERNS = (
    FailurePattern("sandbox_unavailable",
                   r"sandbox is required but failed to initiali[sz]e",
                   "install what the Claude CLI's sandbox needs (bubblewrap and socat) on the "
                   "BOSS host or image, then retry"),
)
#: How the Codex CLI says its Linux sandbox (Landlock + seccomp) could not be applied.
CODEX_FAILURE_PATTERNS = (
    FailurePattern("sandbox_unavailable",
                   r"codex-linux-sandbox\b.{0,80}(?:not found|failed|error)"
                   r"|landlock\w*.{0,60}(?:not supported|not enabled|unavailable|failed)"
                   r"|sandbox\((?:landlockrestrict|seccompinstall|seccompbackend)\)",
                   "run Codex on a kernel with Landlock and seccomp enabled (or give it a host "
                   "that has them), then retry"),
)

#: Scope under which BOSS's own API stores a subscription credential. One constant so the
#: endpoint that writes it and the harness that reads it cannot disagree about the key.
SUBSCRIPTION_SECRET_SCOPE = "subscription"


def _grok_subscription_config_problem(home: Path, system_dir: Path = Path("/etc/grok")) -> str:
    """Refuse config that can route a Grok subscription turn to an API-billed model.

    Grok resolves per-model API credentials before the active OAuth session. Inspect every
    documented config layer, including version overrides, without exposing config values.
    A config we cannot read or parse is unsafe to assume subscription-backed.
    """
    api_route_keys = {"api_key", "env_key", "base_url", "extra_headers", "api_backend"}

    def has_api_route(value: object) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "model" and isinstance(child, dict):
                    if api_route_keys.intersection(child) or any(
                        isinstance(entry, dict) and api_route_keys.intersection(entry)
                        for entry in child.values()
                    ):
                        return True
                if has_api_route(child):
                    return True
        elif isinstance(value, list):
            return any(has_api_route(item) for item in value)
        return False

    for directory, name in (
        (system_dir, "managed_config.toml"),
        (home, "managed_config.toml"),
        (home, "config.toml"),
        (home, "requirements.toml"),
        (system_dir, "requirements.toml"),
    ):
        path = directory / name
        try:
            if not path.exists() and not path.is_symlink():
                continue
            with path.open("rb") as handle:
                config = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            return "Grok configuration cannot be verified for subscription billing."
        if has_api_route(config):
            return ("Grok model configuration can use API billing. Remove its API model "
                    "routing before connecting a subscription.")
    return ""


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
    #: Prefix appended to the prompt as one argv element. `--single=<prompt>` keeps a prompt
    #: beginning with `-` from being mistaken for a Grok option.
    prompt_prefix: str = ""
    #: Flag that selects a model, e.g. "--model". Empty means the CLI has no such flag.
    model_flag: str = ""
    #: HOW TO ASK THE CLI WHICH MODELS THE SIGNED-IN ACCOUNT HAS — never a list of names.
    #: "codex_debug_models", "claude_initialize" or "grok_models"; empty means the CLI cannot
    #: say, and the picker offers only its default plus free text. A list written here went
    #: stale twice: codex showed gpt-5.2-codex/o3 while the account had gpt-6-*, and Claude
    #: offered `fable`, which that account's CLI did not list. See `offered_models`.
    models_catalog: str = ""
    #: Regex a pasted token must match before BOSS stores it. A credential pasted into the wrong
    #: prompt — on boss-1 a 10-character value sat where a Claude setup token belonged — was
    #: stored without a word and surfaced much later as "401 Invalid bearer token" inside a
    #: workflow. Empty means the CLI's token has no checkable shape.
    token_format: str = ""
    #: Seconds before BOSS gives up on a build step. Review steps get review_timeout.
    timeout: int = 2400
    review_timeout: int = 420
    #: Has a governed code task actually completed on this harness?
    verified: bool = False
    #: Fail the stage when the process fails, rather than passing a failure message to the
    #: next stage as if it were the agent's answer.
    fail_on_nonzero: bool = False
    description: str = ""
    #: Env the CLI needs to find its own credentials, on top of the agent env allow-list.
    env_passthrough: tuple[str, ...] = ()
    # --- how BOSS discovers this agent on a customer's machine -----------------
    #: Prints the version. Proves the binary runs, not merely that a file exists.
    version_args: tuple[str, ...] = ("--version",)
    #: Asks the CLI whether it is signed in, e.g. ("login", "status").
    auth_check_args: tuple[str, ...] = ()
    #: Names removed from both the auth probe and child agent. An API key must not make a
    #: subscription session look connected or silently switch its turns to API billing.
    env_exclude: tuple[str, ...] = ()
    #: Require an owner-local CLI session file to exist and be readable, without opening or
    #: copying its contents. This is for CLIs whose API-key fallback masks signed-out state.
    require_local_session_file: bool = False
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
    #:
    #: EMPTY MEANS THE ENVIRONMENT VARIABLE *IS* THE LOGIN — there is no subcommand to run,
    #: the CLI reads the variable on every invocation. claude-code works this way, and
    #: verified against claude 2.1.273: with CLAUDE_CODE_OAUTH_TOKEN set, `claude auth status`
    #: reports `"loggedIn": true, "authMethod": "oauth_token"` with nothing on disk at all.
    #: So for that CLI there is nothing to sign in — only a value to put in the child's
    #: environment, which is what `_env` does.
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
    #: Should this spec also REGISTER a harness of the same name?
    #:
    #: False for claude_code: it already has a better driver. `ClaudeCodeAgent` is a
    #: purpose-written adapter that a governed task has actually completed on, and registering
    #: a generic `claude -p` wrapper under the same name would either collide at import or
    #: quietly replace the good driver with a worse one. What this spec is for is the other
    #: half — detection, the console's Connect button, and the credential route — none of
    #: which the harness registry provides and all of which an operator needs before they can
    #: choose the harness at all.
    registers_harness: bool = True
    #: What the CLI needs on PATH at runtime (harness_registry.RuntimeBinary), registered with
    #: the harness. A CLI the image ships says how ("npm"); one it does not says "operator".
    runtime: tuple = ()
    #: How this CLI says it could not run (harness_registry.FailurePattern), registered with
    #: the harness; harness_failures reads its output against them.
    failure_patterns: tuple = ()

    def supports_read_only(self) -> bool:
        return bool(self.read_only_args)


#: The CLIs BOSS ships a spec for. NONE is marked verified: a spec written from a CLI's
#: documentation is a hypothesis until a governed task completes on it, and presenting an
#: untested adapter as ready is how a registry stops meaning anything.
BUILTIN_CLI_HARNESSES: dict[str, CliHarnessSpec] = {
    # VERIFIED AGAINST claude 2.1.273. This spec does NOT drive Claude Code — see
    # `registers_harness` — it makes Claude Code *connectable*: the console could detect and
    # connect Codex but had only prose for Claude, telling the operator to set an environment
    # variable on the API container and restart it. That is the one thing the wizard exists to
    # remove, and it left the customer who picked Claude Code for a role with no way to pick it.
    #
    # The credential route is the environment variable, and `claude setup-token` mints exactly
    # the long-lived token it wants. That is why Claude was never the CLI with the session
    # problem: a subscription can run a laptop and a server at once because the server gets a
    # token of its own, where Codex has no such command and the whole auth.json has to travel.
    "claude_code": CliHarnessSpec(
        name="claude_code",
        binary="claude",
        args=("-p",),
        # Nothing here drives real work, so there is no write mode to declare. The proof turn
        # runs with every tool disabled — `--tools ""` is the CLI's own way to say that — so
        # connecting can read a model and cannot touch a file.
        write_args=(),
        read_only_args=("--tools", ""),
        # STDIN, NOT ARGV, and `--tools` is the reason. It is VARIADIC — `--tools <tools...>`
        # — so it consumes every argument that follows it, including a prompt appended last:
        #
        #   claude -p --tools "" "Reply with exactly: BOSS_OK"
        #   Error: Input must be provided either through stdin or as a prompt argument
        #
        # Measured against claude 2.1.273, on the first real connect. The prompt on stdin has
        # no trailing argument to be mistaken for a tool name, and `--print` documents stdin
        # as a first-class route rather than a workaround.
        prompt_via="stdin",
        model_flag="--model",
        # Asked of the CLI: the stream-json `initialize` control request answers with the
        # account's models (value + description) without running a model turn — measured at
        # 0.6s on boss-1. Nothing maps names here; the value goes to `--model` unchanged.
        models_catalog="claude_initialize",
        # `claude setup-token` mints a long `sk-ant-…` OAuth token.
        token_format=r"^sk-ant-[A-Za-z0-9_\-]{40,}$",
        # A governed task HAS completed on claude_code; this is the same engine, reached
        # through its own adapter.
        verified=True,
        registers_harness=False,
        description="Claude Code headless (`claude -p`). Authorised by a Claude "
                    "subscription's own login, or an Anthropic API key.",
        env_passthrough=("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR"),
        version_args=("--version",),
        # JSON, so the check is a field rather than a sentence that can be reworded.
        auth_check_args=("auth", "status"),
        auth_ok_pattern=r'"loggedIn":\s*true',
        auth_fail_pattern=r'"loggedIn":\s*false',
        signin_hint="claude auth login",
        install_hint="npm install -g @anthropic-ai/claude-code",
        token_env="CLAUDE_CODE_OAUTH_TOKEN",
        # No token_login_args: the variable is the login. See the field's own note.
        # THE ONE SUBSCRIPTION ROUTE: Claude's own login, run inside BOSS. Nothing is pasted.
        token_hint="sign in inside BOSS: run `boss connect claude`, or press Sign in in Repo "
                   "and models",
        home_env="CLAUDE_CONFIG_DIR",
        default_home="~/.claude",
        docs_url="https://docs.claude.com/en/docs/claude-code/setup",
        entitlement_source_id="anthropic_claude_code",
        entitlement_provider="anthropic",
        runtime=CLAUDE_CLI_RUNTIME,
        failure_patterns=CLAUDE_FAILURE_PATTERNS,
    ),
    # VERIFIED AGAINST codex-cli 0.154.0, not taken from documentation. `codex exec` is the
    # non-interactive mode; `--sandbox` is a real flag with a real read-only value, which is
    # what lets Codex hold "the reviewer cannot edit". Smoke-tested: edits a file in place in
    # ~22s on a ChatGPT-subscription login with no API key.
    "codex": CliHarnessSpec(
        name="codex",
        binary="codex",
        args=("exec", "--skip-git-repo-check", "--color", "never"),
        # `danger-full-access`, AND THE NAME IS DOING IT A DISSERVICE HERE.
        #
        # With `workspace-write`, codex could not write a single file in BOSS's container and
        # said so plainly while exiting 0:
        #
        #     I couldn't write `test_calc.py` because the environment blocked file operations.
        #
        # Its sandbox uses kernel facilities (landlock/seccomp) that a container does not give
        # it, so its guard fails closed and every edit is silently refused. Measured: identical
        # prompt, `workspace-write` wrote nothing and `danger-full-access` wrote the file and
        # ran the tests.
        #
        # The flag's own help says it is "intended solely for running in environments that are
        # externally sandboxed" — which is exactly this one: BOSS runs the agent inside an
        # ephemeral clone, in a container, with a denied-by-default environment that does not
        # carry DATABASE_URL. That isolation is BOSS's to provide, and claude_code already
        # edits files here with no inner sandbox of its own. Codex was the only harness held to
        # a second, broken boundary — so it was the only one that could not do its job.
        write_args=("--sandbox", "danger-full-access"),
        read_only_args=("--sandbox", "read-only"),
        prompt_via="argv",
        model_flag="--model",
        # `codex debug models` (verified on 0.154 and 0.158) prints the catalog the ChatGPT
        # login is entitled to — the same list the CLI's own /model picker shows.
        models_catalog="codex_debug_models",
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
        # THE ONE SUBSCRIPTION ROUTE: Codex's own device login, run inside BOSS. Nothing is
        # pasted, dropped or imported.
        token_hint="sign in inside BOSS: run `boss connect codex`, or press Sign in in Repo "
                   "and models",
        token_source_file="~/.codex/auth.json",
        docs_url="https://developers.openai.com/codex/cli",
        entitlement_source_id="openai_codex_chatgpt",
        entitlement_provider="openai",
        runtime=(RuntimeBinary("codex", "@openai/codex", "npm", "the Codex CLI"), NODE_RUNTIME),
        failure_patterns=CODEX_FAILURE_PATTERNS,
    ),
    "grok": CliHarnessSpec(
        name="grok",
        binary="grok",
        # Official Grok Build headless mode. The CLI session remains in the service user's
        # own GROK_HOME; BOSS never reads or transports auth.json.
        args=("--no-auto-update", "--no-subagents", "--output-format", "plain"),
        write_args=("--sandbox", "workspace", "--always-approve", "--max-turns", "40"),
        read_only_args=("--sandbox", "read-only", "--permission-mode", "plan",
                        "--tools", "read_file,grep,list_dir", "--max-turns", "12"),
        prompt_prefix="--single=",
        model_flag="--model",
        # `grok models` prints "Available models:" then one `* name` / `- name` per line.
        models_catalog="grok_models",
        verified=False,
        fail_on_nonzero=True,
        description="Official xAI Grok Build CLI using the owner-local Grok subscription "
                    "session. Sign in as the BOSS service user.",
        env_passthrough=("GROK_HOME",),
        version_args=("--version",),
        # `grok models` exits 0 and prints cached models even when signed out. The negative
        # patterns and local-session-file check are essential; exit 0 is not proof of auth.
        auth_check_args=("--no-auto-update", "models"),
        auth_ok_pattern=r"(?m)^Available models:\s*$",
        auth_fail_pattern=(r"(?i)you are not authenticated|no auth credentials|"
                           r"auth\.json unreadable|failed to fetch models|"
                           r"\bnot\s+(?:logged\s+in|signed\s+in|authenticated)\b"),
        env_exclude=("XAI_API_KEY", "GROK_MODELS_BASE_URL",
                     "GROK_CLI_CHAT_PROXY_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
                     "ANTHROPIC_API_KEY"),
        require_local_session_file=True,
        signin_hint="grok login --device-auth (as the BOSS user in the API container)",
        install_hint="curl -fsSL https://x.ai/cli/install.sh | bash",
        home_env="GROK_HOME",
        default_home="~/.grok",
        auth_file_name="auth.json",
        docs_url="https://docs.x.ai/build/cli/headless-scripting",
        entitlement_source_id="xai_grok_build",
        entitlement_provider="xai",
        runtime=(RuntimeBinary("grok", "@xai-official/grok", "npm", "the Grok Build CLI"),),
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
        runtime=(RuntimeBinary("cursor-agent", "curl https://cursor.com/install -fsS | bash",
                               "operator", "the Cursor CLI (bring your own)"),),
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
        runtime=(RuntimeBinary("aider", "python -m pip install aider-install && aider-install",
                               "operator", "Aider (bring your own)"),),
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
            if key in ("args", "write_args", "read_only_args", "env_passthrough",
                       "env_exclude") and isinstance(value, list):
                value = tuple(value)
            fields[key] = value
        fields["name"] = name
        try:
            specs[name] = CliHarnessSpec(**fields)
        except Exception as exc:  # noqa: BLE001
            _log.warning("ignoring bad CLI harness override %r: %s", name, exc)
    return specs


class CredentialStoreUnavailable(RuntimeError):
    """The secret store holding a credential could not be read, so whether one exists is
    UNKNOWN. Not "absent": the caller must not act as though nothing is stored.

    `transient` when the store did not answer (unreachable, timed out, 429, 5xx) — the same
    lookup a little later is expected to work, and model_failures.explain says it waits.
    """

    #: The sentence every surface recognises (model_failures.explain). Never carries a value.
    UNREACHABLE = "BOSS could not reach its secret store to read"
    UNREADABLE = "BOSS could not read"

    def __init__(self, name: str, trouble: str, *, transient: bool):
        self.transient = transient
        lead = self.UNREACHABLE if transient else self.UNREADABLE
        super().__init__(f"{lead} the stored {name} ({trouble[:200]}); "
                         + ("it is retried shortly." if transient else
                            "save the credential again in Setup."))


#: How many times a credential lookup is made when the store did not answer, and the pause
#: before each repeat. Short: a blip under load clears in well under a second, and a caller
#: that reads a key nobody stored (the common case) never waits — only a store that FAILED does.
CREDENTIAL_LOOKUP_ATTEMPTS = 3
CREDENTIAL_LOOKUP_BACKOFF_SECONDS = (0.25, 0.75)


def _lookup_once(settings, name: str) -> tuple[Optional[str], list[tuple[str, bool]]]:
    """(value, trouble) for one pass over the store. `trouble` lists every way the store
    failed to ANSWER on this pass, each with whether it may clear by itself; empty means the
    store answered, and a None value then truly means absent."""
    from .secret_backends import (
        SecretNotFound,
        SecretUnavailable,
        backend_for,
        pick_default_backend,
    )
    from .secrets_service import ref_key_for

    trouble: list[tuple[str, bool]] = []
    try:
        kind, passed_over = pick_default_backend(settings)
        # A configured store that did not answer was passed over for the next one, which does
        # not hold what was written to the first. Its silence is trouble, not absence.
        trouble += [(why, True) for why in passed_over]
        store = backend_for(kind, settings)
    except SecretUnavailable as exc:
        return None, [(str(exc), exc.transient)]
    except Exception as exc:  # noqa: BLE001 — e.g. the database is restarting
        return None, [(type(exc).__name__, True)]
    for key in (name, ref_key_for(SUBSCRIPTION_SECRET_SCOPE, name)):
        try:
            value = store.get(key)
        except SecretNotFound:
            continue                       # an answer: not under this key, try the next
        except SecretUnavailable as exc:
            trouble.append((str(exc), exc.transient))
            continue
        except Exception as exc:  # noqa: BLE001 — a driver error is the store not answering
            trouble.append((type(exc).__name__, True))
            continue
        if (value or "").strip():
            return value.strip(), []
    return None, trouble


def stored_credential(settings, name: str, *, strict: bool = False,
                      include_env: bool = True) -> Optional[str]:
    """A CLI credential: the environment first, then this install's secret store.

    Module-level and shared, because two different things shell out to these CLIs. The coding
    harness resolved the stored credential; the CHAT adapter did not, and ran `claude` with
    whatever the API container happened to have in its environment — which on an install that
    keeps its token in Infisical is nothing. So the console chat answered

        IntegrationConfigurationError: OPENAI_API_KEY is not configured

    about a Claude subscription that was connected, proved and stored: `claude -p` said "Not
    logged in", the falling-back adapter correctly read that as "this backend is unavailable
    here", and the operator was handed the second backend's problem instead of the first's.

    Both keys are tried for the same reason as everywhere else: an operator who sets a
    credential themselves uses the plain name, and BOSS's own API namespaces what it writes.

    ABSENT IS AN ANSWER; A STORE THAT DID NOT ANSWER IS NOT. Every failure used to be
    swallowed as "not stored", so on boss-1 (2026-10-04) one discovery stage out of many
    concurrent ones started `claude` with no login while the stored token was fine — the CLI
    said "Not logged in", and the run was failed as a rejected login, between successful calls
    on either side. A lookup the store did not answer is now made again (bounded), and with
    `strict` a store that still does not answer raises `CredentialStoreUnavailable` instead of
    pretending nothing is stored. Without `strict` the answer stays None, logged as a warning.
    """
    direct = os.environ.get(name) if include_env else None
    if direct and direct.strip():
        return direct.strip()
    trouble: list[tuple[str, bool]] = []
    for attempt in range(max(1, CREDENTIAL_LOOKUP_ATTEMPTS)):
        if attempt:
            pauses = CREDENTIAL_LOOKUP_BACKOFF_SECONDS
            time.sleep(pauses[min(attempt - 1, len(pauses) - 1)] if pauses else 0)
        value, trouble = _lookup_once(settings, name)
        if value:
            return value
        if not trouble:
            return None                    # the store answered: nothing stored
        if not any(transient for _, transient in trouble):
            break                          # broken, not busy: asking again cannot help
    said = "; ".join(why for why, _ in trouble)
    transient = any(flag for _, flag in trouble)
    # Names and store errors only — a store error never carries the value (secret_backends).
    _log.warning("could not read the stored %s: %s", name, said)
    if strict:
        raise CredentialStoreUnavailable(name, said, transient=transient)
    return None


def claude_login_token(settings) -> str:
    """The subscription login a `claude` child runs on, or "" when it runs on an API key or on
    nothing. A view of THE ONE RESOLUTION (claude_credential.resolve_claude_credential), kept
    for callers that only ask "is there a login"; a store that did not answer still raises
    `CredentialStoreUnavailable` rather than read as "no login".
    """
    from .claude_credential import KIND_SUBSCRIPTION, resolve_claude_credential

    cred = resolve_claude_credential(settings)
    return cred.value if cred.kind == KIND_SUBSCRIPTION else ""


def _uses_env_route(spec: CliHarnessSpec) -> bool:
    """Is this CLI's credential simply an environment variable it reads on every run?

    Three routes exist and they need telling apart: a login subcommand fed a token
    (`token_login_args`), a credential file that has to be placed (`auth_file_secret`), and
    this one — nothing to run and nothing to write, the variable is the login.
    """
    return bool(spec.token_env) and not spec.token_login_args and not spec.auth_file_secret


def _run_probe(binary: str, args: tuple[str, ...], timeout: int = 20,
               extra_env: Optional[dict] = None,
               exclude_env: tuple[str, ...] = ()) -> tuple[int, str]:
    """Run a short informational command. Never raises: a probe that explodes tells the
    operator less than one that reports what happened."""
    try:
        from .agent_env import agent_environment

        env = agent_environment(os.environ, {"CI": "1", **(extra_env or {})})
        # These are paths to the CLI's own session, not control-plane credentials.
        for name in ("CODEX_HOME", "GROK_HOME"):
            if os.environ.get(name):
                env[name] = os.environ[name]
        for name in exclude_env:
            env.pop(name, None)
        proc = owned_process.run([binary, *args], capture_output=True, text=True, timeout=timeout,
                              env=env)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


_CATALOG_TTL = 600.0
#: The most a catalog read may take before its CLI is killed.
CATALOG_TIMEOUT_SECONDS = 20.0
_CATALOG_RETRY = 60.0
_catalog_cache: dict[str, tuple[float, tuple[str, ...]]] = {}


#: Set while a READ is being served (Repo and models' GET /v1/setup): catalogs then come from
#: the cache only, and a stale one is refreshed on a background thread. A request that spawned
#: `claude -p` to read a catalog held its database transaction open for as long as the CLI took
#: — and the CLI could take for ever — so one hung CLI per page load hung the whole console.
_CACHE_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "boss_catalog_cache_only", default=False)
_refreshing: set[str] = set()
_refresh_lock = threading.Lock()


@contextlib.contextmanager
def catalogs_from_cache_only():
    """Within this block `offered_models` never spawns a CLI: cache, else () and a refresh."""
    token = _CACHE_ONLY.set(True)
    try:
        yield
    finally:
        _CACHE_ONLY.reset(token)


def _refresh_in_background(spec: "CliHarnessSpec") -> None:
    """One refresh per harness at a time, off the caller's thread."""
    with _refresh_lock:
        if spec.name in _refreshing:
            return
        _refreshing.add(spec.name)

    def run() -> None:
        try:
            try:
                found = tuple(_read_model_catalog(spec))
            except Exception:  # noqa: BLE001
                found = ()
            _catalog_cache[spec.name] = (time.monotonic() + (_CATALOG_TTL if found
                                                             else _CATALOG_RETRY), found)
        finally:
            with _refresh_lock:
                _refreshing.discard(spec.name)

    threading.Thread(target=run, name=f"boss-catalog-{spec.name}", daemon=True).start()


def offered_models(name: str) -> tuple[str, ...]:
    """The models the signed-in account can use on this harness, as its own CLI reports them.

    Nothing is written here and nothing is assumed: when the CLI cannot answer (not
    installed, signed out, no catalog command) the answer is empty, and the picker offers the
    CLI's default plus free text rather than a guess that goes stale. Cached ten minutes on
    success, one minute after a failure so a fresh sign-in shows up quickly. Never raises.
    """
    spec = BUILTIN_CLI_HARNESSES.get(str(name or "").lower())
    if spec is None or not spec.models_catalog:
        return ()
    now = time.monotonic()
    cached = _catalog_cache.get(spec.name)
    if cached and now < cached[0]:
        return cached[1]
    if _CACHE_ONLY.get():
        _refresh_in_background(spec)
        return cached[1] if cached else ()
    try:
        found = tuple(_read_model_catalog(spec))
    except Exception:  # noqa: BLE001 — a picker must not break because a CLI did
        _log.warning("could not read %s's model catalog", spec.name, exc_info=True)
        found = ()
    _catalog_cache[spec.name] = (now + (_CATALOG_TTL if found else _CATALOG_RETRY), found)
    return found


def _read_model_catalog(spec: CliHarnessSpec) -> tuple[str, ...]:
    binary = shutil.which(spec.binary)
    if not binary:
        return ()
    reader = {"codex_debug_models": _codex_catalog, "claude_initialize": _claude_catalog,
              "grok_models": _grok_catalog}.get(spec.models_catalog)
    return reader(spec, binary) if reader else ()


def _codex_catalog(spec: CliHarnessSpec, binary: str) -> tuple[str, ...]:
    """The models codex marks for its own picker, in its own order; hidden ones left out."""
    code, output = _run_probe(binary, ("debug", "models"), timeout=20,
                              exclude_env=spec.env_exclude)
    if code != 0 or "{" not in output:
        return ()
    # The probe joins stdout and stderr; the catalog is the JSON object in it.
    data, _ = json.JSONDecoder().raw_decode(output[output.index("{"):])
    models = data.get("models") if isinstance(data, dict) else None
    listed = [m for m in models or [] if isinstance(m, dict)
              and m.get("visibility") == "list" and isinstance(m.get("slug"), str)]
    listed.sort(key=lambda m: m["priority"] if isinstance(m.get("priority"), int) else 10**9)
    return tuple(m["slug"] for m in listed)


def _claude_catalog(spec: CliHarnessSpec, binary: str) -> tuple[str, ...]:
    """Ask Claude Code the way its Agent SDK does: an `initialize` control request.

    The response lists the account's models without a model turn. Its "default" entry is the
    CLI's own default, which the picker's empty option already means, so it is not repeated.
    Run with the same environment as a real run, so the stored subscription token is the
    login it reports for.
    """
    from ..config import get_settings

    env = GenericCliCodeAgent(get_settings(), spec)._env()
    request = json.dumps({"type": "control_request", "request_id": "boss-models",
                          "request": {"subtype": "initialize"}})
    import queue

    proc = owned_process.popen(
        [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
         "--verbose"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        env=env, cwd=os.path.expanduser("~") or "/")
    # A HARD DEADLINE. `readline()` blocks until the CLI writes a line, so a deadline checked
    # between reads never fired when the CLI wrote nothing: the read is on its own thread and
    # this one waits on a queue with a timeout, then kills the process either way.
    lines: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=lambda: [lines.put(l) for l in iter(proc.stdout.readline, "")]
                     + [lines.put("")], daemon=True).start()
    try:
        proc.stdin.write(request + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + CATALOG_TIMEOUT_SECONDS
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return ()
            try:
                line = lines.get(timeout=left)
            except queue.Empty:
                return ()
            if not line:
                return ()
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("type") != "control_response":
                continue
            body = message.get("response") or {}
            body = body.get("response", body)
            return tuple(str(m["value"]) for m in body.get("models") or []
                         if isinstance(m, dict) and m.get("value")
                         and str(m["value"]) != "default")
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _grok_catalog(spec: CliHarnessSpec, binary: str) -> tuple[str, ...]:
    code, output = _run_probe(binary, ("--no-auto-update", "models"), timeout=20,
                              exclude_env=spec.env_exclude)
    if code != 0 or "Available models:" not in output:
        return ()
    listed = output.split("Available models:", 1)[1]
    return tuple(match.group(1) for match in
                 re.finditer(r"(?m)^\s*[*-]\s+(\S+)", listed))


#: `boss connect <name>` for each subscription CLI (setup_pills.CONNECT_NAME is the same map).
_CONNECT_NAME = {"claude_code": "claude", "codex": "codex", "grok": "grok"}

#: How a legacy stored Claude token goes: the governed Disconnect of the Claude Code source
#: (entitlements.disconnect_source → claude_credential.forget_subscription_login), which
#: revokes the stored token and deletes it from its store. Then the in-BOSS sign-in.
LEGACY_TOKEN_FIX = (
    "Remove it with Repo and models › Sources › Claude Code › Disconnect (POST "
    "/v1/entitlements/sources/anthropic_claude_code/disconnect), then run `boss connect claude`.")


def legacy_token_stored(spec: "CliHarnessSpec", settings) -> bool:
    """Does BOSS's secret store hold this CLI's token from the retired import (a Claude
    setup-token), as opposed to the CLI being signed in inside BOSS?

    Such a token is not a sign-in. `claude` reads CLAUDE_CODE_OAUTH_TOKEN ahead of its own
    login, so while it is stored every run — and every proof — uses it, whatever login the
    operator makes. One set in this process's environment (the install's .env) is the
    install's own choice and is not counted here. Never raises; never returns the value.

    The long-lived token BOSS's own Sign in obtained (`claude setup-token` run on the server,
    claude_credential.store_long_lived_token) is stored under the same name and is NOT legacy:
    it is the subscription. Its origin marker tells the two apart."""
    token_env = getattr(spec, "token_env", "")
    if settings is None or not token_env or getattr(spec, "token_login_args", ()) \
            or getattr(spec, "auth_file_secret", "") or os.environ.get(token_env):
        return False
    try:
        if not stored_credential(settings, token_env, include_env=False):
            return False
        from .claude_credential import TOKEN_NAME, boss_obtained_token_stored

        return not (token_env == TOKEN_NAME and boss_obtained_token_stored(settings))
    except Exception:  # noqa: BLE001 — an unreachable store is "absent", not a crash
        return False


def _sign_in_step(spec: "CliHarnessSpec") -> str:
    name = _CONNECT_NAME.get(spec.name)
    if name:
        return (f"sign in inside BOSS: run `boss connect {name}` on the BOSS host, or press "
                f"Sign in in Repo and models (it runs {spec.signin_hint.split(' (')[0]} as the "
                "BOSS user)")
    return f"sign in with: {spec.signin_hint}"


def detect(spec: CliHarnessSpec, settings=None) -> dict:
    """Is this coding agent installed on THIS machine, and is it signed in?

    Detection is the difference between a product and a set of instructions. BOSS cannot ask a
    customer to hand over a subscription credential — a Codex or Cursor login is an OAuth
    session held by that CLI, and there is nothing to paste. What BOSS can do is look for the
    CLI the customer already uses, confirm it is signed in, and record the entitlement.

    Never raises. "Not installed" and "installed but signed out" are answers, and each has a
    different next step — which is the whole point of asking.

    `settings` is optional and only matters for a CLI whose credential is an environment
    variable: the value BOSS stored is not in this process's environment, so without it the
    probe would report "not signed in" about a credential the install genuinely holds — and
    the operator who had just handed it over would be told it had not arrived.
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
    # A STORED TOKEN IS NOT A SIGN-IN. It is what the retired setup-token import left in the
    # secret store, and it used to be injected into the auth probe, so `claude auth status`
    # said signed in while the CLI itself was signed out — and `boss connect claude` was told
    # "already signed in" and then refused. It is reported as `legacy_token`, with the one
    # governed way to remove it, and never counted as signed in.
    probe_env: dict = {}
    legacy = legacy_token_stored(spec, settings)
    result["legacy_token"] = legacy
    result["legacy_token_fix"] = LEGACY_TOKEN_FIX if legacy else None
    boss_token = False
    if spec.name == "claude_code" and settings is not None and not legacy:
        from .claude_credential import boss_obtained_token_stored

        boss_token = boss_obtained_token_stored(settings)
    result["long_lived_token"] = boss_token
    result["token_present"] = bool(credential and (os.environ.get(credential) or legacy
                                                   or boss_token))
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
        if spec.name == "claude_code":
            version = _re.fullmatch(r"\s*(\d+\.\d+\.\d+)\s*\(Claude Code\)\s*", out)
            result["version"] = version.group(1) if version else None
        else:
            result["version"] = out.splitlines()[0][:120]
    if not spec.auth_check_args:
        # No login of its own — installed is the whole check.
        result["authenticated"] = None
        result["ready"] = True
        result["next_step"] = None
        return result
    if spec.require_local_session_file:
        home = os.path.expanduser(os.environ.get(spec.home_env, "") or spec.default_home)
        session_file = os.path.join(home, spec.auth_file_name)
        try:
            session_stat = os.stat(session_file)
            local_session = (os.path.isfile(session_file)
                             and session_stat.st_uid == os.geteuid()
                             and session_stat.st_mode & 0o077 == 0
                             and os.access(session_file, os.R_OK))
        except OSError:
            local_session = False
        if not local_session:
            result["authenticated"] = False
            result["auth_detail"] = "No readable, service-user-owned private CLI session (mode 0600)."
            result["next_step"] = _sign_in_step(spec)
            return result
        if spec.name == "grok":
            config_problem = _grok_subscription_config_problem(Path(home))
            if config_problem:
                result["authenticated"] = False
                result["auth_detail"] = config_problem
                result["next_step"] = config_problem
                return result
    code, out = _run_probe(found, spec.auth_check_args, extra_env=probe_env,
                           exclude_env=spec.env_exclude)
    # The line that ANSWERS the question, not merely the first one printed. These CLIs emit
    # warnings before their status — a fresh CODEX_HOME produces "WARNING: proceeding, even
    # though we could not create PATH aliases…" — and reporting that as `auth_detail` hands the
    # operator a sentence about symlinks when they are debugging a login. The troubleshooting
    # tables in the setup doc are keyed on this field, so it has to be the status.
    if spec.name == "claude_code":
        # The CLI's JSON may contain account details. Parse only the two status fields we
        # need; never return raw stdout/stderr (including malformed output) to an API caller
        # or persist it in an entitlement note.
        try:
            status = json.loads(out)
        except (TypeError, ValueError):
            status = {}
        if not isinstance(status, dict):
            status = {}
        method = status.get("authMethod")
        # The CLI's own words, mapped to BOSS's: a subscription login prints "claude.ai"
        # (2.1.x), never "claude_ai" — matching only the underscore read every real
        # subscription sign-in as "unknown", so the console's Sign in failed after the login
        # had succeeded and Try again then signed it out. Anything unrecognised stays
        # "unknown" (and is never echoed).
        method = (CLAUDE_AUTH_METHODS.get(method, "unknown") if isinstance(method, str)
                  else "unknown")
        signed_in = code == 0 and status.get("loggedIn") is True and method != "unknown"
        result["auth_method"] = method
        result["auth_detail"] = (
            "Signed in with the Claude Code CLI." if signed_in else
            "Not signed in." if status.get("loggedIn") is False else
            "Claude Code authentication status unavailable."
        )
        if boss_token:
            # THE LONG-LIVED TOKEN BOSS OBTAINED (`claude setup-token` on this server) is what
            # every `claude` child runs on — ahead of the CLI's own login — so it is the answer.
            signed_in = True
            result["auth_method"] = "long_lived_token"
            result["auth_detail"] = "BOSS holds a long-lived Claude subscription token."
    else:
        detail = _auth_detail(spec, out)
        result["auth_detail"] = detail or None
        # The denial is checked first and wins outright: "Not logged in" contains "logged in",
        # so a positive-only test reports a signed-out CLI as ready.
        denied = bool(spec.auth_fail_pattern) and bool(_re.search(spec.auth_fail_pattern, out or ""))
        signed_in = (code == 0 and not denied and bool(spec.auth_ok_pattern)
                     and bool(_re.search(spec.auth_ok_pattern, out or "")))
    result["authenticated"] = signed_in
    result["ready"] = signed_in
    if legacy:
        result["next_step"] = f"BOSS holds a legacy Claude token. {LEGACY_TOKEN_FIX}"
    elif not signed_in:
        # ONE ROUTE: the provider's own login, inside BOSS. Never "set a token" or "copy a
        # file" — those imports are refused (setup_actions.subscription_import_refusal).
        result["next_step"] = _sign_in_step(spec)
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
    """The most informative line of an auth-status output, as a SENTENCE.

    The line-picking below assumes the CLI prints prose. `claude auth status` prints JSON,
    which is better to test against and worse to read: the most informative line is literally
    `"loggedIn": false,` — a fragment, shown to a customer, on the screen where they are
    trying to work out whether their subscription is connected.
    """
    import re as _re

    lines = [line.strip() for line in (out or "").splitlines() if line.strip()]
    if not lines:
        return ""
    spoken = _spoken_auth_detail(out)
    if spoken:
        return spoken
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


#: `claude auth status` authMethod → BOSS's name for it. "claude.ai" is what a subscription
#: login prints; "claude_ai" is kept so records written with BOSS's name still read.
CLAUDE_AUTH_METHODS: dict[str, str] = {
    "claude.ai": "claude_ai", "claude_ai": "claude_ai", "oauth": "oauth",
    "oauth_token": "oauth_token", "api_key": "api_key",
}


def _spoken_auth_detail(out: str) -> str:
    """Turn a JSON auth status into the sentence a person would say about it."""
    try:
        parsed = json.loads((out or "").strip())
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(parsed, dict) or "loggedIn" not in parsed:
        return ""
    if not parsed.get("loggedIn"):
        return "Not signed in."
    method = {"oauth_token": "a long-lived token", "api_key": "an API key",
              "oauth": "a subscription login", "claude_ai": "a Claude subscription"}.get(
        CLAUDE_AUTH_METHODS.get(str(parsed.get("authMethod") or ""), ""), "")
    source = parsed.get("apiKeySource")
    if method and source:
        return f"Signed in with {method} ({source})."
    return f"Signed in with {method}." if method else "Signed in."


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
    return [detect(spec, settings) for spec in load_specs(settings).values()]


class CliHarnessUnavailable(RuntimeError):
    """The binary is not installed, or the step needs something the CLI cannot do."""


#: A CLI run's model-proxy tokens (`_env`) are revoked when `run_role` ends (model_proxy).
_proxy_scoped = _model_proxy.scoped()


class GenericCliCodeAgent:
    """Drive any coding CLI that edits files in the working directory."""

    def __init__(self, settings, spec: CliHarnessSpec) -> None:
        self.settings = settings
        self.spec = spec
        self.engine = spec.name

    @property
    def capabilities(self):
        """What a generic CLI gives BOSS (engine_contract): a read-only mode where the spec has
        one, nothing else BOSS can rely on. Sessions, a turn transcript and cost are supplied
        by BOSS's fallbacks; a reviewer on a CLI without read-only mode judges a copy."""
        from .engine_contract import EngineCapabilities

        return EngineCapabilities(read_only=self.spec.supports_read_only())

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
        from .agent_env import NEVER, agent_environment

        env = agent_environment(os.environ)
        # A CLI signed in with its own OAuth needs its own home/config to find the session.
        for name in self.spec.env_passthrough:
            value = os.environ.get(name)
            if value and not (self.spec.name in {"codex", "claude_code"}
                              and name.upper() in NEVER):
                env[name] = value
        for name in self.spec.env_exclude:
            env.pop(name, None)
        env.setdefault("HOME", os.environ.get("HOME", ""))
        env["CI"] = "1"
        # THE VARIABLE IS THE LOGIN, so it has to be here on every run. `env_passthrough`
        # copies it when the operator set it in this process's environment; this also covers
        # the credential BOSS itself stored, which is not in the environment at all.
        if self.spec.name == "claude_code":
            # Claude's login OR the console's Anthropic API key: one resolution for every
            # `claude` BOSS starts (claude_credential), never a second lookup order here.
            from .claude_credential import resolve_claude_credential

            try:
                resolve_claude_credential(self.settings).apply(env)
            except CredentialStoreUnavailable:
                pass           # as before: what the environment holds, logged by the lookup
        elif _uses_env_route(self.spec) and not env.get(self.spec.token_env):
            token = self._resolve_secret(self.spec.token_env)
            if token:
                env[self.spec.token_env] = token
        # A PROVIDER API KEY NEVER REACHES THE CLI (model_proxy): an Anthropic, OpenAI or
        # OpenRouter key passed through above becomes a run token for the loopback proxy, with
        # the base URL the CLI reads. A CLI's own login (a token, an auth file) is untouched.
        return _model_proxy.shield_env(env, settings=self.settings)

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
            argv.append(f"{self.spec.prompt_prefix}{prompt}" if self.spec.prompt_prefix
                        else prompt)
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
        env_route = _uses_env_route(self.spec)
        if not has_file_route and not env_route and (
                not self.spec.token_env or not self.spec.token_login_args):
            return {"authorised": None, "reason": "this CLI has no token route"}
        found = detect(self.spec, self.settings)
        if not found["installed"]:
            return {"authorised": False, "reason": "not installed"}
        if env_route:
            # Nothing to sign in and nothing to write: the credential is handed to the CLI in
            # its environment by `_env` on each run. So "authorised" is exactly "we have one",
            # and there is no state left behind that could go stale.
            if self._resolve_secret(self.spec.token_env):
                return {"authorised": True,
                        "reason": "the stored token is given to the CLI on every run"}
            # SHORT ON PURPOSE. The caller pairs this with detection's own next step, which
            # already carries the hint — saying it twice produced a 422 that stated the same
            # sentence in the same breath, once in brackets.
            return {"authorised": bool(found["authenticated"]),
                    "reason": f"no {self.spec.token_env} stored for this install"}
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
            proc = owned_process.run(
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
        return stored_credential(self.settings, name)

    def _resolve_token(self) -> Optional[str]:
        """The travelling token: the environment first, then the deployment's secret store.

        Delegates, rather than repeating the lookup. The copy that used to live here read the
        store under the plain key ONLY, so a token stored by BOSS's own API — which namespaces
        what it writes — was invisible to the one code path that needed it.
        """
        return self._resolve_secret(self.spec.token_env)

    def _preflight(self, *, read_only: bool, read_only_by_copy: bool = False) -> None:
        """What every session of this CLI checks before it starts, whichever driver runs it
        (this class, or a purpose-written one such as codex_code_agent)."""
        from . import harness_failures

        # A run whose engine already could not run starts no new session (harness_failures).
        harness_failures.raise_if_tripped()
        if read_only and not self.spec.supports_read_only() and not read_only_by_copy:
            # "The reviewer cannot edit" is an invariant of the governed pipeline. A CLI with
            # no read-only mode cannot hold it, so it is refused for the step rather than
            # trusted to behave — unless the engine contract runs the reviewer on a throwaway
            # copy of the tree (engine_contract._isolated_copy).
            raise CliHarnessUnavailable(
                f"{self.spec.name!r} has no read-only mode, so it cannot run a review step "
                f"without being able to edit the code it is reviewing. Set "
                f"BOSS_CODE_REVIEW_ENGINE to a harness that can (any API lane), or give this "
                f"one read_only_args via BOSS_CLI_HARNESSES_JSON."
            )
        if self.spec.require_local_session_file:
            found = detect(self.spec, self.settings)
            if not found["ready"]:
                raise CliHarnessUnavailable(
                    f"{self.spec.name} is not authenticated for this service user. "
                    f"{found.get('next_step') or self.spec.signin_hint}"
                )
        # Sign in from the stored credential if this deployment has one and the CLI is not
        # already signed in — a token for some CLIs, the CLI's own auth file for others.
        # Cheap after the first call: `detect` short-circuits on "already signed in".
        if ((self.spec.token_env or self.spec.auth_file_secret)
                and not getattr(self, "_auth_checked", False)):
            self._auth_checked = True
            self._auth_state = self.ensure_authorised()

    def _raise_harness_verdict(self, *, stdout: str, stderr: str, summary: str,
                               ok: bool) -> None:
        """THE CLI COULD NOT RUN, IN ITS OWN WORDS (harness_failures: its sandbox, a runtime
        binary; on a failed exit also its login, its account, a crash). Not the agent's work:
        the stage stops with the harness verdict instead of handing on what it wrote blind."""
        from . import harness_failures

        verdict = harness_failures.classify(f"{stderr or ''}\n{stdout or ''}",
                                            engine=self.engine, where="stream")
        if verdict is None and not ok:
            verdict = harness_failures.classify(f"{summary}\n{stderr or ''}",
                                                engine=self.engine, where="engine")
        if verdict is not None:
            raise harness_failures.trip(verdict)

    @tidies_workspace
    @_proxy_scoped
    def run_role(self, prompt: str, cwd: Path, *, read_only: bool = False,
                 model: Optional[str] = None, resume_session_id: Optional[str] = None,
                 **_ignored) -> dict:
        from .llm_telemetry import record_llm_call
        from .scheduling import low_priority

        self._preflight(read_only=read_only,
                        read_only_by_copy=bool(_ignored.get("_contract_read_only_copy")))
        import re as _re
        import tempfile

        argv = self._argv(prompt, read_only=read_only, model=model)
        final_path = None
        if self.spec.final_message_flag:
            fd, final_path = tempfile.mkstemp(prefix="boss-cli-final-", suffix=".txt")
            os.close(fd)
            # Inserted before the prompt, which `_argv` appended last.
            insert_at = len(argv) - 1 if self.spec.prompt_via == "argv" else len(argv)
            argv[insert_at:insert_at] = [self.spec.final_message_flag, final_path]
        timeout = self.spec.review_timeout if read_only else self.spec.timeout
        # THE STAGE'S WALL-TIME DEADLINE bounds this session as it bounds every engine's.
        from .code_agent import _cap_to_deadline

        timeout = _cap_to_deadline(int(timeout), read_only=read_only)
        started = time.monotonic()
        try:
            proc = owned_process.run(
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
                    proc = owned_process.run(
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
        # A TRANSCRIPT IN THE COMMON SCHEMA even from a CLI that streams nothing BOSS can read:
        # what it was asked and what it said, so every stage of every engine has one.
        try:
            from .llm_telemetry import record_agent_turns

            record_agent_turns([{"direction": "completion", "turn_index": 0,
                                 "content": summary[:8000], "ok": ok}],
                               engine_name=self.engine, model=model or "", prompt=prompt)
        except Exception:  # noqa: BLE001 — a transcript never fails a build
            pass
        self._raise_harness_verdict(stdout=proc.stdout or "", stderr=proc.stderr or "",
                                    summary=summary, ok=ok)
        if not ok:
            _log.warning("%s exited %s: %s", self.spec.name, proc.returncode,
                         (proc.stderr or "")[:300])
            if self.spec.fail_on_nonzero:
                raise CliHarnessUnavailable(
                    f"{self.spec.name} exited {proc.returncode}: {summary[:300]}")
        return {
            "summary": summary[:8000] or f"({self.spec.name} produced no output)",
            "usage": {"model": model or self.spec.name},
            "cost_usd_micros": 0,
            "cost_unreported": True,
            "turns": 1,
            # The MODEL, never the harness's name: a caller passes this back as `model=` on a
            # follow-up turn, and `--model codex` is not a model (crun QA static fix).
            "model": model or "",
            "exit_code": proc.returncode,
            "stop_reason": "done" if ok else "error",
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
            # THE AGENT IT RUNS AS: its operator's working instructions (agent-loadout §4.8).
            + (f"\n{spec.agent_instructions}\n" if getattr(spec, "agent_instructions", "")
               else "")
            + "\nEdit files in place. Do NOT create a branch, commit, or push — BOSS handles "
              "git, the pull request and the merge gate.\n"
        )
        out = self.run_role(prompt, sandbox.path)
        from .verification_target import run_until_failure
        results = run_until_failure(sandbox, spec.test_commands)
        return CodeAgentResult(
            summary=out["summary"], diff=sandbox.diff(), diff_summary=sandbox.diff_summary(),
            tests=results, tests_passed=all(r.ok for r in results) if results else False,
            cost_usd_micros=0, usage=out["usage"], transcript=out["summary"],
            turns=1, engine=self.engine,
        )
