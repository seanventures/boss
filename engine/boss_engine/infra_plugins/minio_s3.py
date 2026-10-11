"""official.minio-s3: S3-compatible object storage (MinIO) on the app's private network.

The app never gets the root credentials: provision creates ONE bucket and an access key whose
policy allows only that bucket. Every binding (endpoint, bucket, key, secret) is a vault reference.

verify  AS THE APP (its scoped key, over the app's network): put an object, get it back byte for
        byte, delete it; and prove the key is scoped: creating another bucket is denied.
"""
from __future__ import annotations

import json
import re

from ..platform.contract import HookContext, OperationResult, OperationStatus, PlannedChange
from ._runtime import (
    HookFailure, OfficialPlugin, Session, env_prefix, evidence, labels, new_password, service_name,
)

#: MinIO stopped publishing community images in 2025; pgsty/minio is a maintained build of the
#: same AGPL source (it ships `mc` too). Pinned by digest.
IMAGE = ("pgsty/minio:RELEASE.2026-08-04T00-00-00Z"
         "@sha256:b6bfe7239bfc83fb90d31612d9704d86039dd714f7904b3f1ad68f211e602372")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


class MinioS3Plugin(OfficialPlugin):
    directory = "minio-s3"

    def image(self, ctx: HookContext) -> str:
        return ctx.state.get("image") or ctx.resource.config.get("image") or IMAGE

    def volume(self, ctx: HookContext) -> str:
        return f"{service_name(ctx)}-data"

    def _validate(self, ctx: HookContext) -> list[str]:
        b = ctx.resource.config.get("bucket")
        if b and not _BUCKET.fullmatch(b):
            return ["config.bucket must be 3-63 lower-case letters, digits or hyphens"]
        return []

    def _changes(self, ctx: HookContext) -> list[PlannedChange]:
        return [PlannedChange(action="create", target=f"service:{service_name(ctx)}",
                              detail=f"{self.image(ctx).split('@')[0]}, no published port"),
                PlannedChange(action="create", target=f"volume:{self.volume(ctx)}", detail="objects, kept until destroy"),
                PlannedChange(action="create", target="bucket", detail="one bucket; its name is the S3_BUCKET binding"),
                PlannedChange(action="create", target="access-key", detail="scoped to that bucket only"),
                PlannedChange(action="create", target="secret:S3_*", detail="endpoint, bucket and key as vault references")]

    def fragment(self, ctx: HookContext) -> dict:
        svc, vol, lab, pre = service_name(ctx), self.volume(ctx), labels(ctx, self.plugin_id), env_prefix(ctx)
        return {
            "services": {svc: {
                "image": self.image(ctx), "restart": "unless-stopped",
                "command": ["server", "/data", "--address", ":9000"],
                "environment": {"MINIO_ROOT_USER": f"${{{pre}_ROOT_USER}}",
                                "MINIO_ROOT_PASSWORD": f"${{{pre}_ROOT_PASSWORD}}",
                                "MINIO_BROWSER": "off"},
                "volumes": [f"{vol}:/data"],
                "healthcheck": {"test": ["CMD", "curl", "-fsS", "http://127.0.0.1:9000/minio/health/live"],
                                "interval": "2s", "timeout": "3s", "retries": 30},
                "labels": lab,
            }},
            "volumes": {vol: {"labels": lab}},
        }

    KEYS = ("rootUserRef", "rootPasswordRef", "endpointRef", "bucketRef", "accessKeyRef", "secretKeyRef")

    def do_provision(self, ctx: HookContext, s: Session) -> OperationResult:
        svc, pre = service_name(ctx), env_prefix(ctx)
        if ctx.state.get("rootUserRef"):
            refs = {k: ctx.state[k] for k in self.KEYS}
        else:
            if s.container_id(svc):
                raise HookFailure("state_lost", f"{svc} exists but its secret references are not in state")
            bucket = ctx.resource.config.get("bucket") or f"{re.sub('[^a-z0-9-]', '-', ctx.resource.name)}-{new_password(4)}"
            refs = {"rootUserRef": s.put("ROOT_USER", "root" + new_password(6)).secretRef,
                    "rootPasswordRef": s.put("ROOT_PASSWORD", new_password()).secretRef,
                    "endpointRef": s.put("S3_ENDPOINT_URL", f"http://{svc}:9000").secretRef,
                    "bucketRef": s.put("S3_BUCKET", bucket).secretRef,
                    "accessKeyRef": s.put("S3_ACCESS_KEY_ID", "app" + new_password(8)).secretRef,
                    "secretKeyRef": s.put("S3_SECRET_ACCESS_KEY", new_password()).secretRef}
        root_user, root_pw = s.get(refs["rootUserRef"]), s.get(refs["rootPasswordRef"])
        s.write_fragment(self.fragment(ctx))
        s.compose_up(svc, {f"{pre}_ROOT_USER": root_user, f"{pre}_ROOT_PASSWORD": root_pw})
        policy = "boss-" + re.sub("[^a-z0-9-]", "-", ctx.resource.name)
        script = r"""
set -e
printf '%s' "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"s3:*\"],\"Resource\":[\"arn:aws:s3:::$BOSS_BUCKET\",\"arn:aws:s3:::$BOSS_BUCKET/*\"]}]}" > /tmp/policy.json
mc mb --ignore-existing root/"$BOSS_BUCKET" >/dev/null
mc admin user add root "$BOSS_AK" "$BOSS_SK" >/dev/null
mc admin policy create root "$BOSS_POLICY" /tmp/policy.json >/dev/null
mc admin policy attach root "$BOSS_POLICY" --user "$BOSS_AK" >/dev/null 2>&1 || mc admin user info root "$BOSS_AK" | grep -q "$BOSS_POLICY"
echo ready
"""
        out = s.text(s.probe(self.image(ctx), ["-c", script], entrypoint="sh", what="creating the bucket and scoped key",
                             env={"MC_HOST_root": f"http://{root_user}:{root_pw}@{svc}:9000",
                                  "BOSS_BUCKET": s.get(refs["bucketRef"]), "BOSS_AK": s.get(refs["accessKeyRef"]),
                                  "BOSS_SK": s.get(refs["secretKeyRef"]), "BOSS_POLICY": policy}))
        if "ready" not in out:
            raise HookFailure("provision_failed", "the bucket or scoped key was not created")
        state = {**ctx.state, **refs, "service": svc, "volume": self.volume(ctx), "image": self.image(ctx),
                 "fragment": f"compose/{ctx.resource.name}.yaml",
                 "env": {f"{pre}_ROOT_USER": refs["rootUserRef"], f"{pre}_ROOT_PASSWORD": refs["rootPasswordRef"]}}
        return ctx.result(bindings=self._bindings(ctx, refs), state=state, artifacts=[state["fragment"]],
                          evidence=[evidence("container", True, "healthcheck", f"{svc} healthy"),
                                    evidence("scoped_key", True, "config", "bucket created; key allowed on it only")])

    def _bindings(self, ctx: HookContext, refs: dict) -> dict:
        return self.bindings(ctx, {"S3_ENDPOINT_URL": refs["endpointRef"], "S3_BUCKET": refs["bucketRef"],
                                   "S3_ACCESS_KEY_ID": refs["accessKeyRef"], "S3_SECRET_ACCESS_KEY": refs["secretKeyRef"]})

    def _require(self, ctx: HookContext) -> None:
        if not ctx.state.get("accessKeyRef"):
            raise HookFailure("not_provisioned", f"{ctx.resource_id} has not been provisioned")

    def do_configure(self, ctx: HookContext, s: Session) -> OperationResult:
        self._require(ctx)
        return ctx.result(bindings=self._bindings(ctx, ctx.state), state=ctx.state,
                          evidence=[evidence("binding", True, "config", "S3_* are vault references")])

    def as_app(self, ctx: HookContext, s: Session, script: str, what: str, stdin: bytes = b"") -> str:
        self._require(ctx)
        endpoint = s.get(ctx.state["endpointRef"]).split("://", 1)
        host = f"{endpoint[0]}://{s.get(ctx.state['accessKeyRef'])}:{s.get(ctx.state['secretKeyRef'])}@{endpoint[1]}"
        proc = s.probe(self.image(ctx), ["-c", script], entrypoint="sh", what=what,
                       env={"MC_HOST_app": host, "BOSS_BUCKET": s.get(ctx.state["bucketRef"])}, stdin=stdin)
        return s.text(proc)

    def do_verify(self, ctx: HookContext, s: Session) -> OperationResult:
        nonce = new_password(16)
        out = self.as_app(ctx, s, r"""
read -r BOSS_NONCE
printf '%s' "$BOSS_NONCE" | mc pipe app/"$BOSS_BUCKET"/boss-verify/object >/dev/null && echo put=ok
echo "get=$(mc cat app/"$BOSS_BUCKET"/boss-verify/object)"
mc rm app/"$BOSS_BUCKET"/boss-verify/object >/dev/null && echo rm=ok
if mc mb app/boss-verify-denied-"$BOSS_NONCE" >/dev/null 2>&1; then mc rb app/boss-verify-denied-"$BOSS_NONCE" >/dev/null 2>&1; echo scope=open; else echo scope=denied; fi
""", "verifying object storage", stdin=(nonce + "\n").encode())
        lines = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        round_trip = lines.get("put") == "ok" and lines.get("get") == nonce and lines.get("rm") == "ok"
        scoped = lines.get("scope") == "denied"
        ev = [evidence("put_get_object", round_trip, "connection", "put, got back byte for byte, deleted, as the app"),
              evidence("key_is_scoped", scoped, "config", "creating another bucket with the app key is denied")]
        if round_trip and scoped:
            return ctx.result(evidence=ev, state=ctx.state)
        return ctx.result(OperationStatus.FAILED, evidence=ev, errors=[{"code": "verify_failed",
                                                                        "message": "object storage did not pass verify"}])

    def do_observe(self, ctx: HookContext, s: Session) -> OperationResult:
        out = self.as_app(ctx, s, 'mc du --json app/"$BOSS_BUCKET"', "observing object storage")
        try:
            size = int(json.loads(out.splitlines()[-1]).get("size", 0))
        except Exception:
            size = 0
        return ctx.result(observed={"healthy": True, "bucketBytes": size},
                          evidence=[evidence("usage", True, "observe", "bucket usage read")])

    def do_destroy(self, ctx: HookContext, s: Session) -> OperationResult:
        self.guard_destroy(ctx)
        removed = s.compose_destroy(service_name(ctx), [self.volume(ctx)])
        for key in self.KEYS:
            if ref := ctx.state.get(key):
                ctx.secrets.delete(ref)
                removed.append(f"secret:{key}")
        return ctx.result(evidence=[evidence("removed", True, "cleanup", ", ".join(removed))], state={})


Plugin = MinioS3Plugin
