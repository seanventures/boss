"""Where the harness points: a stack it boots itself, or a live install such as boss-1.

The two share one shape — an API URL, an OWNER key that maps to `ceo` and a WORKER key that maps
to `system`, a console URL and its password — because the failures this harness exists to catch
only happen with transport auth on and real key→principal mapping. On boss-1 the console key is
`ceo`, the worker key is `system`, and that is precisely why the agent wizard (posting as
`vp_agents`) and "Advance now" (needing the worker principal) answered 403 there while every
in-process TestClient suite, which runs with auth off, stayed green. A target that silently
turned auth off would reproduce the green suites, not the customer.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Target:
    kind: str                      # "local" (booted here) or "live" (an existing install)
    api_url: str
    owner_key: str                 # maps to the owner principal (ceo) — what the console uses
    worker_key: Optional[str]      # maps to the worker principal (system); None on a live
                                   # install where the harness is not given it
    project_id: str = "boss-dogfood"
    owner_principal: str = "ceo"
    console_url: Optional[str] = None
    console_password: Optional[str] = None
    model_stub_url: Optional[str] = None   # None on a live install with real models
    fakes_url: Optional[str] = None
    smtp_port: Optional[int] = None
    # Only for a stack the harness booted: lets it play the clock (a 7-day grace timer). Never
    # set for a live install, whose database the harness does not touch.
    database_url: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    @property
    def real_models(self) -> bool:
        return self.model_stub_url is None

    def describe(self) -> str:
        models = "real models" if self.real_models else f"scripted models at {self.model_stub_url}"
        return (f"{self.kind} target {self.api_url} (project {self.project_id}, {models}, "
                f"console {self.console_url or 'not walked'})")


class TargetError(RuntimeError):
    """A target that cannot be used, with the sentence that says how to fix it."""


def live_target_from_env(env: Optional[dict[str, str]] = None) -> Target:
    """A live install, named entirely by environment so no secret ever lands in argv or a file.

    BOSS_E2E_API_URL, BOSS_E2E_OWNER_KEY are required; BOSS_E2E_WORKER_KEY, BOSS_E2E_PROJECT,
    BOSS_E2E_CONSOLE_URL and BOSS_E2E_CONSOLE_PASSWORD are optional. On boss-1 these come from
    ~/boss/.env: BOSS_API_KEY is the owner key, BOSS_WORKER_API_KEY the worker key.
    """
    env = dict(os.environ if env is None else env)
    missing = [name for name in ("BOSS_E2E_API_URL", "BOSS_E2E_OWNER_KEY") if not env.get(name)]
    if missing:
        raise TargetError(
            "A live target needs " + " and ".join(missing) + ". On boss-1 run "
            "`set -a; . ~/boss/.env; set +a` and then export BOSS_E2E_API_URL=http://127.0.0.1:"
            "$BOSS_API_PORT BOSS_E2E_OWNER_KEY=$BOSS_API_KEY BOSS_E2E_WORKER_KEY="
            "$BOSS_WORKER_API_KEY before scripts/e2e.sh --target live.")
    target = Target(
        kind="live",
        api_url=env["BOSS_E2E_API_URL"].rstrip("/"),
        owner_key=env["BOSS_E2E_OWNER_KEY"],
        worker_key=env.get("BOSS_E2E_WORKER_KEY") or None,
        project_id=env.get("BOSS_E2E_PROJECT") or "boss-dogfood",
        owner_principal=env.get("BOSS_E2E_OWNER_PRINCIPAL") or "ceo",
        console_url=(env.get("BOSS_E2E_CONSOLE_URL") or "").rstrip("/") or None,
        console_password=env.get("BOSS_E2E_CONSOLE_PASSWORD") or None,
    )
    if not target.worker_key:
        target.notes.append(
            "No worker key: runs advance only as fast as the install's own worker polls.")
    return target
