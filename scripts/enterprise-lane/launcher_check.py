"""THE ENTERPRISE LANE'S LAUNCHER CHECK (scripts/enterprise-lane.sh). No model is called.

Runs INSIDE the installed api container, the way the Setup engine check runs a login's proof
(engine_policy.run_check): under a build's owner and sandbox identity, so every command goes
through the real launcher (`boss-sandbox-exec run <uid> -- ...`) as the per-run uid, with that
run's HOME and the egress fence. Gap #12 of the enterprise RCA was missed because an earlier check
started Codex's bubblewrap directly instead of through this launcher.

Checks, in order:
  * the sandbox identity binds (isolation on, a uid from the pool);
  * engine_inner_sandbox.check: Codex's own sandbox (`codex sandbox`, workspace-write) starts
    through the launcher under the container's seccomp/AppArmor profiles;
  * engine_policy.codex_sandbox_proof: the same sandbox writes its workspace and every toolchain
    cache, and reaches the configured package registry (the private npm mirror through the fence).

Prints one JSON object. Exit 0 only when all three hold. Nothing printed carries a credential:
the sandbox output is redacted and cut short.
"""
from __future__ import annotations

import json
import sys
import time


def main() -> int:
    from boss_engine.identity.redaction import redact_text
    from boss_engine.services import (engine_inner_sandbox, engine_policy, owned_process,
                                      sandbox_identity)

    project = sys.argv[1] if len(sys.argv) > 1 else "boss-dogfood"
    owner_id = f"entlane_{int(time.time())}"
    out: dict = {"isolated": False, "uid": None, "inner": None, "proof": None, "error": ""}
    try:
        with owned_process.owner_scope("engine_check", owner_id, project_id=project, fresh=True):
            with sandbox_identity.bind(owner_id, project_id=project) as ident:
                out["isolated"] = ident is not None
                out["uid"] = getattr(ident, "uid", None)
                out["network_fenced"] = sandbox_identity.network_fenced()
                out["inner"] = engine_inner_sandbox.check("codex", run=owned_process.run)
                out["proof"] = engine_policy.codex_sandbox_proof("codex")
    except Exception as exc:  # noqa: BLE001 — reported, never a traceback with an environment
        out["error"] = f"{type(exc).__name__}: {exc}"
    text = redact_text(json.dumps(out, default=str))
    print(text[:4000])
    inner = out.get("inner") or {}
    proof = out.get("proof") or {}
    ok = (out["isolated"] and inner.get("ok") and proof.get("sandbox_proven")
          and proof.get("network") and not out["error"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
