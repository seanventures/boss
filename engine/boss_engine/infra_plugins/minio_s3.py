"""official.minio-s3: S3-compatible object storage (MinIO) on the app's private network.

The app never gets the root credentials: provision creates ONE bucket and an access key whose
policy allows only that bucket. Endpoint, bucket, key and secret are the app's bindings (vault
references). The root password is generated INSIDE the container on first start and kept in its
own volume; admin work runs inside the container, so the root credentials never leave the host.

verify  AS THE APP (its scoped key, over the app's network): put an object, get it back byte for
        byte, delete it; and prove the key is scoped: creating another bucket is denied.
"""
from __future__ import annotations

import json
import re

from ..platform.contract import HookContext, OperationResult, OperationStatus, PlannedChange
from ._runtime import (
    HookFailure, OfficialPlugin, Session, env_name, evidence, labels, new_password, service_name,
)

#: MinIO no longer publishes community images anonymously (minio/minio and quay.io/minio/minio
#: refuse anonymous pulls). The default is Chainguard's MinIO from its own registry: built from the
#: upstream source, multi-arch (amd64 + arm64), signed with Sigstore cosign and shipped with an
#: SBOM, so its provenance is verifiable:
#:   cosign verify cgr.dev/chainguard/minio@<digest> \
#:     --certificate-oidc-issuer=https://token.actions.githubusercontent.com \
#:     --certificate-identity=https://github.com/chainguard-images/images/.github/workflows/release.yaml@refs/heads/main
#: Pinned by the multi-arch index digest (minio RELEASE.2026-09-22T19-25-18Z). An operator with a
#: MinIO subscription image (or any S3-compatible MinIO build) supplies it as `config.image`,
#: pinned by digest. The image must provide `sh` and `mc`.
IMAGE = ("cgr.dev/chainguard/minio:latest"
         "@sha256:7df55d002b698e4494a131b848dd553361bbe58b7e19f62d9623c29af1ba9ccf")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
#: The root password lives in its own volume, mounted as the (non-root) image user's home, so the
#: image's own user can write it whichever image runs.
ROOT_DIR = "/home/nonroot"
START = (f'f={ROOT_DIR}/.boss-root-password; [ -s "$$f" ] || (cat /proc/sys/kernel/random/uuid '
         '/proc/sys/kernel/random/uuid | tr -d "\\n-" > "$$f"); export MINIO_ROOT_USER=boss-root '
         'MINIO_ROOT_PASSWORD="$$(cat "$$f")"; exec minio server /data --address :9000')
#: inside the container: an mc alias for the root user, from the password file
ROOT = f'export MC_HOST_root="http://boss-root:$(cat {ROOT_DIR}/.boss-root-password)@127.0.0.1:9000"; '
BINDINGS = ("S3_ENDPOINT_URL", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")


class MinioS3Plugin(OfficialPlugin):
    directory = "minio-s3"

    def image(self, ctx: HookContext) -> str:
        return ctx.resource.config.get("image") or IMAGE

    def volumes(self, ctx: HookContext) -> tuple[str, str]:
        return f"{service_name(ctx)}-data", f"{service_name(ctx)}-root"

    def _validate(self, ctx: HookContext) -> list[str]:
        b = ctx.resource.config.get("bucket")
        if b and not _BUCKET.fullmatch(b):
            return ["config.bucket must be 3-63 lower-case letters, digits or hyphens"]
        return []

    def _changes(self, ctx: HookContext) -> list[PlannedChange]:
        data, root = self.volumes(ctx)
        return [PlannedChange(action="create", target=f"service:{service_name(ctx)}",
                              detail=f"{self.image(ctx).split('@')[0]}, no published port"),
                PlannedChange(action="create", target=f"volume:{data}", detail="objects, kept until destroy"),
                PlannedChange(action="create", target=f"volume:{root}", detail="the root password, never leaves the host"),
                PlannedChange(action="create", target="bucket", detail="one bucket; its name is the S3_BUCKET binding"),
                PlannedChange(action="create", target="access-key", detail="scoped to that bucket only"),
                PlannedChange(action="create", target="binding:S3_*", detail="endpoint, bucket and key as vault references")]

    def fragment(self, ctx: HookContext) -> dict:
        svc, lab = service_name(ctx), labels(ctx, self.plugin_id)
        data, root = self.volumes(ctx)
        return {
            "services": {svc: {
                "image": self.image(ctx), "restart": "unless-stopped",
                "entrypoint": ["sh", "-c", START],
                "environment": {"MINIO_BROWSER": "off"},
                "volumes": [f"{data}:/data", f"{root}:{ROOT_DIR}"],
                "healthcheck": {"test": ["CMD-SHELL", ROOT.replace("$", "$$") + "mc ready root >/dev/null"],
                                "interval": "2s", "timeout": "3s", "retries": 30},
                "labels": lab,
            }},
            "volumes": {data: {"labels": lab}, root: {"labels": lab}},
        }

    def do_provision(self, ctx: HookContext, s: Session) -> OperationResult:
        svc = service_name(ctx)
        bucket = ctx.resource.config.get("bucket") or f"{re.sub('[^a-z0-9-]', '-', ctx.resource.name)}-{new_password(4)}"
        refs = self.store_bindings(ctx, s, {
            "S3_ENDPOINT_URL": f"http://{svc}:9000", "S3_BUCKET": bucket,
            "S3_ACCESS_KEY_ID": lambda: "app" + new_password(8), "S3_SECRET_ACCESS_KEY": lambda: new_password()})
        fragment = self.fragment(ctx)
        s.compose_up(fragment, svc)
        cid = s.require_container(svc)
        values = {b: s.get(refs[env_name(ctx, b)]) for b in BINDINGS}
        policy = "boss-" + re.sub("[^a-z0-9-]", "-", ctx.resource.name)
        script = ROOT + r"""set -e
printf '%s' "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"s3:*\"],\"Resource\":[\"arn:aws:s3:::$BOSS_BUCKET\",\"arn:aws:s3:::$BOSS_BUCKET/*\"]}]}" > /tmp/policy.json
for i in 1 2 3 4 5 6 7 8 9 10; do mc admin info root >/dev/null 2>&1 && break; sleep 1; done
mc mb --ignore-existing root/"$BOSS_BUCKET" >/dev/null
mc admin user add root "$BOSS_AK" "$BOSS_SK" >/dev/null
mc admin policy create root "$BOSS_POLICY" /tmp/policy.json >/dev/null
mc admin policy attach root "$BOSS_POLICY" --user "$BOSS_AK" >/dev/null 2>&1 || mc admin user info root "$BOSS_AK" | grep -q "$BOSS_POLICY"
rm -f /tmp/policy.json
echo ready
"""
        out = s.exec(cid, ["sh", "-c", script], what="creating the bucket and scoped key",
                     env={"BOSS_BUCKET": values["S3_BUCKET"], "BOSS_AK": values["S3_ACCESS_KEY_ID"],
                          "BOSS_SK": values["S3_SECRET_ACCESS_KEY"], "BOSS_POLICY": policy})
        if "ready" not in out.stdout:
            raise HookFailure("provision_failed", "the bucket or scoped key was not created")
        state = {**ctx.state, "bindingRefs": refs, "service": svc, "image": self.image(ctx), "fragment": fragment}
        return ctx.result(bindings=self.as_bindings(refs), state=state,
                          evidence=[evidence("container", True, "healthcheck", f"{svc} healthy"),
                                    evidence("scoped_key", True, "config", "bucket created; the app key may use it only")])

    def _require(self, ctx: HookContext) -> None:
        if not self.ref(ctx, "S3_ACCESS_KEY_ID"):
            raise HookFailure("not_provisioned", f"{ctx.resource_id} has not been provisioned")

    def do_configure(self, ctx: HookContext, s: Session) -> OperationResult:
        self._require(ctx)
        return ctx.result(bindings=self.as_bindings(ctx.state["bindingRefs"]), state=ctx.state,
                          evidence=[evidence("binding", True, "config", "S3_* are vault references")])

    def as_app(self, ctx: HookContext, s: Session, script: str, what: str, stdin: str = "") -> str:
        self._require(ctx)
        v = {b: s.get(self.ref(ctx, b)) for b in BINDINGS}
        scheme, rest = v["S3_ENDPOINT_URL"].split("://", 1)
        host = f"{scheme}://{v['S3_ACCESS_KEY_ID']}:{v['S3_SECRET_ACCESS_KEY']}@{rest}"
        out = s.probe(self.image(ctx), ["-c", script], entrypoint="sh", what=what, stdin=stdin,
                      env={"MC_HOST_app": host, "BOSS_BUCKET": v["S3_BUCKET"]})
        return s.text(out)

    def do_verify(self, ctx: HookContext, s: Session) -> OperationResult:
        nonce = new_password(16)
        out = self.as_app(ctx, s, r"""
read -r BOSS_NONCE
printf '%s' "$BOSS_NONCE" | mc pipe app/"$BOSS_BUCKET"/boss-verify/object >/dev/null && echo put=ok
echo "get=$(mc cat app/"$BOSS_BUCKET"/boss-verify/object)"
mc rm app/"$BOSS_BUCKET"/boss-verify/object >/dev/null && echo rm=ok
if mc mb app/boss-verify-denied-"$BOSS_NONCE" >/dev/null 2>&1; then mc rb app/boss-verify-denied-"$BOSS_NONCE" >/dev/null 2>&1; echo scope=open; else echo scope=denied; fi
""", "verifying object storage", stdin=nonce + "\n")
        lines = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        round_trip = lines.get("put") == "ok" and lines.get("get") == nonce and lines.get("rm") == "ok"
        scoped = lines.get("scope") == "denied"
        ev = [evidence("put_get_object", round_trip, "connection", "put, got back byte for byte, deleted, as the app"),
              evidence("key_is_scoped", scoped, "config", "creating another bucket with the app key is denied")]
        if round_trip and scoped:
            return ctx.result(evidence=ev, state=ctx.state)
        return ctx.result(OperationStatus.FAILED, evidence=ev,
                          errors=[{"code": "verify_failed", "message": "object storage did not pass verify"}])

    def do_observe(self, ctx: HookContext, s: Session) -> OperationResult:
        out = self.as_app(ctx, s, 'mc du --json app/"$BOSS_BUCKET"', "observing object storage")
        try:
            size = int(json.loads(out.strip().splitlines()[-1]).get("size", 0))
        except Exception:
            size = 0
        return ctx.result(observed={"healthy": True, "bucketBytes": size},
                          evidence=[evidence("usage", True, "observe", "bucket usage read")])

    def do_destroy(self, ctx: HookContext, s: Session) -> OperationResult:
        self.guard_destroy(ctx)
        removed = s.compose_destroy(service_name(ctx), self.volumes(ctx)) + self.delete_bindings(ctx)
        return ctx.result(evidence=[evidence("removed", True, "cleanup", ", ".join(removed) or "nothing left")],
                          state={})
