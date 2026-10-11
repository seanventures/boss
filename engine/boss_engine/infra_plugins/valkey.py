"""official.valkey: a Valkey CACHE on the app's private network.

A cache, never a store: no RDB snapshots, no AOF, no volume, no backups, LRU eviction. A request
for backups is invalid; the plan says so whatever `persistence` says.

provision  `res-<name>`, no published port. Its only secret is the app's REDIS_URL binding; the
           service reads the same binding (`${REDIS_URL}`) and takes its password from it, so
           there is no second secret to keep and a deploy that writes the app's env file starts
           it unchanged.
verify     AS THE APP (REDIS_URL, over the app's network): AUTH, SET with a TTL, GET, DEL; and
           that persistence really is off.
"""
from __future__ import annotations

from ..platform.contract import HookContext, OperationResult, OperationStatus, PlannedChange
from ._runtime import (
    HookFailure, OfficialPlugin, Session, env_name, evidence, labels, new_password, service_name,
)

IMAGE = "valkey/valkey:8.1.3-alpine@sha256:d827e7f7552cdee40cc7482dbae9da020f42bc47669af6f71182a4ef76a22773"
#: The password is the userinfo of the binding: redis://default:<pw>@host:6379/0
_PW = 'pw="$${BOSS_REDIS_URL#*://default:}"; pw="$${pw%%@*}"'


class ValkeyPlugin(OfficialPlugin):
    directory = "valkey"

    def image(self, ctx: HookContext) -> str:
        return ctx.resource.config.get("image") or IMAGE

    def _validate(self, ctx: HookContext) -> list[str]:
        if ctx.resource.backups:
            return ["Valkey is a cache, never a durable store: BOSS does not back it up. "
                    "Keep data you need in official.postgres"]
        return []

    def _changes(self, ctx: HookContext) -> list[PlannedChange]:
        return [PlannedChange(action="create", target=f"service:{service_name(ctx)}",
                              detail=f"{self.image(ctx).split('@')[0]}, no persistence, no published port"),
                PlannedChange(action="create", target="binding:REDIS_URL", detail="vault reference")]

    def _risks(self, ctx: HookContext) -> list[str]:
        return ["a cache: every key is lost on restart or eviction; never the only copy of anything"]

    def fragment(self, ctx: HookContext) -> dict:
        mem = ctx.resource.config.get("maxMemoryMb", 128)
        start = (f"{_PW}; printf 'requirepass %s\\n' \"$$pw\" > /tmp/boss-valkey.conf && unset BOSS_REDIS_URL pw && "
                 f"exec valkey-server /tmp/boss-valkey.conf --save '' --appendonly no --maxmemory {mem}mb "
                 "--maxmemory-policy allkeys-lru")
        return {"services": {service_name(ctx): {
            "image": self.image(ctx),
            "restart": "unless-stopped",
            "user": "valkey",
            "entrypoint": ["sh", "-c", start],
            "environment": {"BOSS_REDIS_URL": f"${{{env_name(ctx, 'REDIS_URL')}}}"},
            "healthcheck": {"test": ["CMD-SHELL", "valkey-cli -p 6379 ping 2>&1 | grep -q -e PONG -e NOAUTH"],
                            "interval": "2s", "timeout": "3s", "retries": 30},
            "labels": labels(ctx, self.plugin_id),
        }}}

    def do_provision(self, ctx: HookContext, s: Session) -> OperationResult:
        svc = service_name(ctx)
        refs = self.store_bindings(ctx, s, {"REDIS_URL": lambda: f"redis://default:{new_password()}@{svc}:6379/0"})
        name = env_name(ctx, "REDIS_URL")
        fragment = self.fragment(ctx)
        s.compose_up(fragment, svc, {name: s.get(refs[name])})
        state = {**ctx.state, "bindingRefs": refs, "service": svc, "image": self.image(ctx), "fragment": fragment}
        return ctx.result(bindings=self.as_bindings(refs), state=state,
                          evidence=[evidence("container", True, "healthcheck", f"{svc} healthy")])

    def do_configure(self, ctx: HookContext, s: Session) -> OperationResult:
        if not self.ref(ctx, "REDIS_URL"):
            raise HookFailure("not_provisioned", f"{ctx.resource_id} has not been provisioned")
        return ctx.result(bindings=self.as_bindings(ctx.state["bindingRefs"]), state=ctx.state,
                          evidence=[evidence("binding", True, "config", "REDIS_URL is a vault reference")])

    def cli(self, ctx: HookContext, s: Session, commands: list[str], what: str) -> list[str]:
        if not self.ref(ctx, "REDIS_URL"):
            raise HookFailure("not_provisioned", f"{ctx.resource_id} has not been provisioned")
        script = " && ".join(f'valkey-cli --no-auth-warning -u "$BOSS_URL" {c}' for c in commands)
        out = s.probe(self.image(ctx), ["-c", script], env={"BOSS_URL": s.get(self.ref(ctx, "REDIS_URL"))},
                      entrypoint="sh", what=what)
        return [line.strip() for line in s.text(out).splitlines()]

    def do_verify(self, ctx: HookContext, s: Session) -> OperationResult:
        key, value = f"boss:verify:{new_password(4)}", new_password(4)
        out = self.cli(ctx, s, [f"SET {key} {value} EX 30", f"GET {key}", f"DEL {key}",
                                "CONFIG GET appendonly", "CONFIG GET save"], "verifying valkey")
        set_get = out[:3] == ["OK", value, "1"]
        not_durable = out[3:5] == ["appendonly", "no"] and out[5:7] in (["save", ""], ["save"])
        ev = [evidence("set_get", set_get, "connection", "AUTH, SET with TTL, GET and DEL as the app"),
              evidence("cache_not_durable", not_durable, "config", "appendonly no, no RDB snapshots: a cache")]
        if set_get and not_durable:
            return ctx.result(evidence=ev, state=ctx.state)
        return ctx.result(OperationStatus.FAILED, evidence=ev,
                          errors=[{"code": "verify_failed", "message": "valkey did not pass verify"}])

    def do_observe(self, ctx: HookContext, s: Session) -> OperationResult:
        stats = dict(line.split(":", 1) for line in self.cli(ctx, s, ["INFO stats"], "observing valkey") if ":" in line)
        return ctx.result(observed={"healthy": True, "hits": int(stats.get("keyspace_hits", 0)),
                                    "misses": int(stats.get("keyspace_misses", 0)),
                                    "evicted": int(stats.get("evicted_keys", 0))},
                          evidence=[evidence("stats", True, "observe", "INFO stats read")])

    def do_destroy(self, ctx: HookContext, s: Session) -> OperationResult:
        self.guard_destroy(ctx)
        removed = s.compose_destroy(service_name(ctx)) + self.delete_bindings(ctx)
        return ctx.result(evidence=[evidence("removed", True, "cleanup", ", ".join(removed) or "nothing left")],
                          state={})
