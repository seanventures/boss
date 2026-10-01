"""CodexCliAdapter — the Codex CLI as a raw completion backend for BOSS's own loop.

The coding pipeline could already run on Codex; agent chat, meetings and discovery could not,
because those surfaces call an OpenAI-compatible API and Codex is a CLI. So `BOSS_PROVIDER_LOCK
=codex` moved the pipeline and quietly left every conversation on whatever `OPENAI_*`
credentials happened to be configured — which is the opposite of what an operator setting one
lock believes they have done, and it would have made a "we run entirely on Codex" validation
come out green while another vendor answered every chat.

Everything about rendering a conversation, demanding one JSON object back and reshaping it is
shared with the Claude path and lives in `CliChatBrain`. Only three things are Codex's own:

  * the prompt goes on **stdin**, which `codex exec` reads when the argument is `-`. The Claude
    path had to grow a size test for this, because a long conversation plus ~90 tool schemas
    exceeds Linux's 128 KiB cap on a single argv entry and dies as "Argument list too long".
    Codex avoids the cliff entirely rather than straddling it.
  * the final message comes from `--output-last-message`, not stdout. With `--json` stdout is
    a JSONL event stream, which is where the turn's token usage lives — measured, not assumed:
    `turn.completed` carries input, cached-input, cache-write, output and reasoning counts.
  * `--sandbox read-only` and `--ignore-user-config`, so a brain that is supposed to be pure
    reasoning cannot edit the filesystem and does not inherit the operator's own Codex
    configuration. This is the same intent as the Claude path's `--tools ""`.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from typing import Optional

from ..config import Settings
from .claude_cli_adapter import CliChatBrain
from .scheduling import low_priority

_log = logging.getLogger(__name__)


class CodexCliAdapter(CliChatBrain):
    """OpenAI Codex (`codex exec`) as a completion backend."""

    _session_default = "codex-cli"
    _engine_name = "codex"

    def _codex_bin(self) -> str:
        return (getattr(self.settings, "codex_cli_path", "") or "codex").strip() or "codex"

    def _child_env(self) -> dict:
        """The CLI's environment, with the credential this install holds restored if needed.

        Same gap as the Claude path, with codex's own route: what travels is the CLI's whole
        auth file, so the fix is to make sure it is on disk before asking a question rather
        than to pass a token. `ensure_authorised` is the harness's own idempotent placement of
        it and is cheap once the file is there.
        """
        try:
            from .cli_harness import GenericCliCodeAgent, load_specs

            spec = load_specs(self.settings).get("codex")
            if spec is not None:
                GenericCliCodeAgent(self.settings, spec).ensure_authorised()
        except Exception as exc:  # noqa: BLE001 — the CLI may already be signed in
            _log.info("could not restore the codex credential for chat (%s)",
                      type(exc).__name__)
        from .agent_env import agent_environment

        env = agent_environment(os.environ)
        # The auth file may live under a configured Codex home. This is a path, not the
        # control plane's credential; the actual credential is restored by the harness.
        if os.environ.get("CODEX_HOME"):
            env["CODEX_HOME"] = os.environ["CODEX_HOME"]
        return env

    def _scratch_dir(self) -> str:
        directory = (self.settings.repo_root / ".local-logs" / "codex-scratch").resolve()
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory)

    def _invoke(self, prompt: str) -> dict:
        timeout = int(getattr(self.settings, "codex_cli_timeout", 240) or 240)
        # The final message goes to a file rather than being dug out of stdout, which under
        # --json is an event stream rather than an answer.
        handle, last_message = tempfile.mkstemp(prefix="codex-final-", suffix=".txt")
        os.close(handle)
        cmd = [
            self._codex_bin(), "exec",
            "--skip-git-repo-check",
            "--color", "never",
            # Do not persist a session for what is one stateless completion, and do not read
            # the operator's own config — BOSS supplies the whole instruction.
            "--ephemeral",
            "--ignore-user-config",
            # Pure reasoning. BOSS executes tools; this process must not touch the disk.
            "--sandbox", "read-only",
            "--output-last-message", last_message,
            # Events as JSONL, purely for the usage on `turn.completed`. Without it a turn
            # reports no tokens at all, and a backend that appears to cost nothing quietly
            # corrupts every cost comparison BOSS makes between providers.
            "--json",
        ]
        model = self.model_override or (getattr(self.settings, "codex_cli_model", "") or "")
        if model:
            cmd += ["--model", model]
        cmd.append("-")  # read the prompt from stdin
        try:
            proc = subprocess.run(
                low_priority(cmd), input=prompt, capture_output=True, text=True,
                timeout=timeout, cwd=self._scratch_dir(), env=self._child_env(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "codex CLI not found. Install it (npm install -g @openai/codex) and ensure "
                "`codex` is on PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"codex CLI timed out after {timeout}s") from exc
        finally:
            result = ""
            try:
                with open(last_message, "r", encoding="utf-8") as fh:
                    result = fh.read().strip()
            except OSError:
                pass
            try:
                os.unlink(last_message)
            except OSError:
                pass

        if proc.returncode != 0:
            # The CLI's own stderr can quote an access token back when auth is what failed, so
            # only the status and a short stdout excerpt travel. Same rule as the harness.
            raise RuntimeError(
                f"codex CLI exited {proc.returncode}: {(proc.stdout or '')[-300:]}")
        if not result:
            raise RuntimeError("codex CLI produced no final message")
        return {"result": result, "stdout": proc.stdout or "", "session_id": self._session_default}

    @staticmethod
    def _turn_usage(stdout: str) -> dict:
        """Usage off the last `turn.completed` event in the JSONL stream."""
        usage: dict = {}
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{") or "turn.completed" not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
        return usage

    def _usage(self, data: dict) -> dict:
        """Codex's own numbers, mapped onto the split BOSS prices against.

        `input_tokens` is the whole prompt and `cached_input_tokens` is the part of it that was
        served from cache, so genuinely-new input is the difference. Folding them together
        would price cache reads as full-price input and make cache savings invisible — the
        same mistake the Claude path had to be corrected for (BOSS-280).

        Reasoning tokens are billed as output, so they belong in `completion_tokens` rather
        than in a field of their own that nothing prices.
        """
        usage = self._turn_usage(data.get("stdout") or "")
        cache_read = int(usage.get("cached_input_tokens") or 0)
        cache_write = int(usage.get("cache_write_input_tokens") or 0)
        prompt = max(int(usage.get("input_tokens") or 0) - cache_read, 0)
        completion = (int(usage.get("output_tokens") or 0)
                      + int(usage.get("reasoning_output_tokens") or 0))
        return {
            "model": (self.model_override
                      or (getattr(self.settings, "codex_cli_model", "") or "") or "codex-cli"),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "total_tokens": prompt + cache_read + cache_write + completion,
        }


def make_codex_chat_adapter(settings: Settings, *, model: Optional[str] = None):
    """Codex with the OpenAI adapter behind it, on the same terms as the Claude path.

    An infrastructure failure — binary missing, not signed in, timed out — is not an answer,
    and falling back gets the operator a reply. A bad turn from the model is an answer of
    sorts and belongs to the caller's retry logic.
    """
    from .claude_cli_adapter import FallingBackAdapter, openai_fallback_usable
    from .openai_adapter import OpenAIAdapter

    codex = CodexCliAdapter(settings, model=model)
    if not openai_fallback_usable(settings):
        # See openai_fallback_usable: falling back to an adapter with no credential turns
        # "codex exited 1" into "OPENAI_API_KEY is not configured", which is true of a
        # provider the operator deliberately did not choose.
        return codex
    return FallingBackAdapter(codex, OpenAIAdapter(settings))
