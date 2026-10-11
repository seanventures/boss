"""official.valkey: a Valkey CACHE on the app's private network.

A cache, never a store: no RDB snapshots, no AOF, no volume, no backups, an LRU eviction policy.
A request for backups is invalid; the plan says so for any request that sets persistence.

provision  `res-<name>` with a password (vault) and no published port.
verify     AS THE APP (REDIS_URL, over the app's network): AUTH, SET with a TTL, GET, DEL; and that
           persistence really is off.
"""
from __future__ import annotations

from ..platform.contract import HookContext, OperationResult, OperationStatus, PlannedChange
from ._runtime import (
    HookFailure, OfficialPlugin, Session, env_prefix, evidence, labels, new_password, service_name,
)

IMAGE = "valkey/valkey:8.1.3-alpine@sha256:d827e7f7552cdee40cc7482dbae9da020f42bc47669af6f71182a4ef76a22773"


class ValkeyPlugin(OfficialPlugin):
    directory = "valkey"

    def image(self, ctx: HookContext) -> str:
        return ctx.state.get("image") or ctx.resource.config.get("image") or IMAGE

    def _validate(self, ctx: HookContext) -> list[str]:
        if ctx.resource.backups:
            return ["Valkey is a cache, never a durable store: BOSS does not back it up. "
                    "Keep data you need in official.postgres"]
        return []

    def _changes(self, ctx: HookContext) -> list[PlannedChange]:
        return [PlannedChange(action="create", target=f"service:{service_name(ctx)}",
                              detail=f"{self.image(ctx).split('@')[0]}, no persistence, no published port"),
                PlannedChange(action="create", target="secret:REDIS_URL", detail="vault reference")]

    def _risks(self, ctx: HookContext) -> list[str]:
        return ["a cache: every key is lost on restart or eviction; never the only copy of anything"]

    def fragment(self, ctx: HookContext) -> dict:
        mem = ctx.resource.config.get("maxMemoryMb", 128)
        start = ("printf 'requirepass %s\\n' \"$$VALKEY_PASSWORD\" > /tmp/boss-valkey.conf && "
                 f"exec valkey-server /tmp/boss-valkey.conf --save '' --appendonly no --maxmemory {mem}mb "
                 "--maxmemory-policy allkeys-lru")
        return {"services": {service_name(ctx): {
            "image": self.image(ctx),
            "restart": "unless-stopped",
            "user": "valkey",
            "entrypoint": ["sh", "-c"],
            "command": [start],
            "environment": {"VALKEY_PASSWORD": f"${{{env_prefix(ctx)}_PASSWORD}}"},
            "healthcheck": {"test": ["CMD-SHELL", 'REDISCLI_AUTH="$$VALKEY_PASSWORD" valkey-cli ping | grep -q PONG'],
                            "interval": "2s", "timeout": "3s", "retries": 30},
            "labels": labels(ctx, self.plugin_id),
        }}}

    def do_provision(self, ctx: HookContext, s: Session) -> OperationResult:
        svc = service_name(ctx)
        if ctx.state.get("passwordRef"):
            pw, refs = s.get(ctx.state["passwordRef"]), {k: ctx.state[k] for k in ("passwordRef", "urlRef")}
        else:
            pw = new_password()
            refs = {"passwordRef": s.put("PASSWORD", pw).secretRef,
                    "urlRef": s.put("REDIS_URL", f"redis://default:{pw}@{svc}:6379/0").secretRef}
        s.write_fragment(self.fragment(ctx))
        s.compose_up(svc, {f"{env_prefix(ctx)}_PASSWORD": pw})
        state = {**ctx.state, **refs, "service": svc, "image": self.image(ctx),
                 "fragment": f"compose/{ctx.resource.name}.yaml", "env": {f"{env_prefix(ctx)}_PASSWORD": refs["passwordRef"]}}
        return ctx.result(bindings=self.bindings(ctx, {"REDIS_URL": refs["urlRef"]}), state=state,
                          artifacts=[state["fragment"]], evidence=[evidence("container", True, "healthcheck", f"{svc} healthy")])

    def do_configure(self, ctx: HookContext, s: Session) -> OperationResult:
        if not ctx.state.get("urlRef"):
            raise HookFailure("not_provisioned", f"{ctx.resource_id} has not been provisioned")
        return ctx.result(bindings=self.bindings(ctx, {"REDIS_URL": ctx.state["urlRef"]}), state=ctx.state,
                          evidence=[evidence("binding", True, "config", "REDIS_URL is a vault reference")])

    def cli(self, ctx: HookContext, s: Session, script: str, what: str) -> list[str]:
        if not ctx.state.get("urlRef"):
            raise HookFailure("not_provisioned", f"{ctx.resource_id} has not been provisioned")
        proc = s.probe(self.image(ctx), ["-c", 'valkey-cli --no-auth-warning -u "$BOSS_URL" ' + script],
                       env={"BOSS_URL": s.get(ctx.state["urlRef"])}, entrypoint="sh", what=what)
        return [line.strip() for line in s.text(proc).splitlines()]

    def do_verify(self, ctx: HookContext, s: Session) -> OperationResult:
        key, value = f"boss:verify:{new_password(4)}", new_password(4)
        out = self.cli(ctx, s, f"SET {key} {value} EX 30 && valkey-cli --no-auth-warning -u \"$BOSS_URL\" GET {key} && "
                       f"valkey-cli --no-auth-warning -u \"$BOSS_URL\" DEL {key} && "
                       "valkey-cli --no-auth-warning -u \"$BOSS_URL\" CONFIG GET appendonly && "
                       "valkey-cli --no-auth-warning -u \"$BOSS_URL\" CONFIG GET save", "verifying valkey")
        set_get = len(out) >= 3 and out[0] == "OK" and out[1] == value and out[2] == "1"
        not_durable = "appendonly" in out and out[out.index("appendonly") + 1] == "no" and out[-1] == ""
        ev = [evidence("set_get", set_get, "connection", "AUTH, SET with TTL, GET and DEL as the app"),
              evidence("cache_not_durable", not_durable, "config", "appendonly no, no RDB snapshots: a cache")]
        if set_get and not_durable:
            return ctx.result(evidence=ev, state=ctx.state)
        return ctx.result(OperationStatus.FAILED, evidence=ev,
                          errors=[{"code": "verify_failed", "message": "valkey did not pass verify"}])

    def do_observe(self, ctx: HookContext, s: Session) -> OperationResult:
        out = self.cli(ctx, s, "INFO stats", "observing valkey")
        stats = dict(line.split(":", 1) for line in out if ":" in line)
        observed = {"healthy": True, "hits": int(stats.get("keyspace_hits", 0)),
                    "misses": int(stats.get("keyspace_misses", 0)), "evicted": int(stats.get("evicted_keys", 0))}
        return ctx.result(observed=observed, evidence=[evidence("stats", True, "observe", "INFO stats read")])

    def do_destroy(self, ctx: HookContext, s: Session) -> OperationResult:
        self.guard_destroy(ctx)
        removed = s.compose_destroy(service_name(ctx))
        for key in ("urlRef", "passwordRef"):
            if ref := ctx.state.get(key):
                ctx.secrets.delete(ref)
                removed.append(f"secret:{key}")
        return ctx.result(evidence=[evidence("removed", True, "cleanup", ", ".join(removed))], state={})


Plugin = ValkeyPlugin
