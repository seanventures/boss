"""official.caddy: the app's HTTP entry point, with TLS whenever a hostname exists.

The one service that faces the network, so the plan marks its ports public unless they bind to
loopback, and a request must say `publicAccess: true` (an approval) to bind beyond it.

  hostname set, tls auto      ACME certificate (Let's Encrypt / ZeroSSL); HTTP redirects to HTTPS
  hostname set, tls internal  Caddy's local CA (also the default for a bare IP address)
  hostname set, tls off       plain HTTP on that hostname
  no hostname                 plain HTTP on :80

verify  an HTTP(S) request THROUGH Caddy (a probe on the app's network, so it works wherever the
        Docker endpoint is): the response carries the route header Caddy adds and is not a
        502/503/504, i.e. Caddy reached the app. With TLS, the certificate chain is checked
        (against Caddy's local root for `internal`).
"""
from __future__ import annotations

import ipaddress
import re
import time

from ..platform.contract import HookContext, OperationResult, OperationStatus, PlannedChange, PortSpec
from ._runtime import HookFailure, OfficialPlugin, Session, evidence, labels, service_name

IMAGE = "caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d"
CURL = "curlimages/curl:8.16.0@sha256:463eaf6072688fe96ac64fa623fe73e1dbe25d8ad6c34404a669ad3ce1f104b6"
_UPSTREAM = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}:\d{1,5}$")
_HOST = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9-]{1,63}\.)*[A-Za-z0-9-]{1,63}$")
LOOPBACK = {"127.0.0.1", "::1"}


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class CaddyPlugin(OfficialPlugin):
    directory = "caddy"

    def cfg(self, ctx: HookContext) -> dict:
        c = dict(ctx.resource.config)
        host = c.get("hostname")
        c.setdefault("tls", ("internal" if host and _is_ip(host) else "auto") if host else "off")
        c.setdefault("bindAddress", "0.0.0.0" if ctx.resource.public else "127.0.0.1")
        c.setdefault("httpPort", 80)
        c.setdefault("httpsPort", 443)
        c.setdefault("verifyPath", "/")
        return c

    def tls(self, ctx: HookContext) -> bool:
        c = self.cfg(ctx)
        return bool(c.get("hostname")) and c["tls"] != "off"

    def public_allowed(self, ctx: HookContext) -> bool:
        return True  # ingress: public is the point, behind publicAccess (an approval)

    def _validate(self, ctx: HookContext) -> list[str]:
        c, out = self.cfg(ctx), []
        if not _UPSTREAM.fullmatch(str(c.get("upstream", ""))):
            out.append("config.upstream must be <compose service>:<port>, e.g. app:8000")
        if c.get("hostname") and not _HOST.fullmatch(c["hostname"]):
            out.append("config.hostname must be a DNS name or an IP address")
        if c["tls"] == "auto" and c.get("hostname") and _is_ip(c["hostname"]):
            out.append("an IP address cannot get a public certificate; use tls: internal")
        if c["bindAddress"] not in LOOPBACK and not ctx.resource.public:
            out.append("binding beyond loopback publishes the app: set publicAccess: true (needs approval)")
        return out

    def _ports(self, ctx: HookContext) -> list[PortSpec]:
        c = self.cfg(ctx)
        public = c["bindAddress"] not in LOOPBACK
        ports = [PortSpec(name="http", containerPort=80, hostPort=c["httpPort"], public=public)]
        if self.tls(ctx):
            ports.append(PortSpec(name="https", containerPort=443, hostPort=c["httpsPort"], public=public))
        return ports

    def _changes(self, ctx: HookContext) -> list[PlannedChange]:
        c = self.cfg(ctx)
        site = c.get("hostname") or ":80"
        return [PlannedChange(action="create", target=f"service:{service_name(ctx)}",
                              detail=f"{IMAGE.split('@')[0]} on {c['bindAddress']}"),
                PlannedChange(action="create", target=f"route:{site}",
                              detail=f"-> {c['upstream']}, TLS {c['tls'] if c.get('hostname') else 'off'}")]

    def caddyfile(self, ctx: HookContext) -> str:
        c = self.cfg(ctx)
        host = c.get("hostname")
        if not host:
            site, tls = ":80", ""
        elif c["tls"] == "off":
            site, tls = f"http://{host}", ""
        else:
            site, tls = host, ("\n\ttls internal" if c["tls"] == "internal" else "")
        email = f"\n\temail {c['acmeEmail']}" if c.get("acmeEmail") else ""
        return (f"{{\n\tadmin off{email}\n}}\n\n{site} {{{tls}\n\treverse_proxy {c['upstream']} {{\n"
                f"\t\theader_down X-Boss-Route \"{ctx.resource.name}\"\n\t}}\n}}\n")

    def volume(self, ctx: HookContext) -> str:
        return f"{service_name(ctx)}-data"

    def fragment(self, ctx: HookContext) -> dict:
        c, svc, lab = self.cfg(ctx), service_name(ctx), labels(ctx, self.plugin_id)
        ports = [f"{c['bindAddress']}:{c['httpPort']}:80"]
        if self.tls(ctx):
            ports.append(f"{c['bindAddress']}:{c['httpsPort']}:443")
        return {"services": {svc: {"image": IMAGE, "restart": "unless-stopped", "ports": ports,
                                   "configs": [{"source": f"{svc}-caddyfile", "target": "/etc/caddy/Caddyfile"}],
                                   "volumes": [f"{self.volume(ctx)}:/data"], "labels": lab}},
                "configs": {f"{svc}-caddyfile": {"content": self.caddyfile(ctx)}},
                "volumes": {self.volume(ctx): {"labels": lab}}}

    def do_provision(self, ctx: HookContext, s: Session) -> OperationResult:
        svc = service_name(ctx)
        s.write_fragment(self.fragment(ctx))
        s.compose_up(svc, {}, wait=True, timeout=90)
        c = self.cfg(ctx)
        scheme, port = ("https", c["httpsPort"]) if self.tls(ctx) else ("http", c["httpPort"])
        url = f"{scheme}://{c.get('hostname') or c['bindAddress']}:{port}/"
        state = {**ctx.state, "service": svc, "volume": self.volume(ctx), "url": url,
                 "fragment": f"compose/{ctx.resource.name}.yaml"}
        return ctx.result(state=state, artifacts=[state["fragment"]],
                          evidence=[evidence("container", True, "healthcheck", f"{svc} running")])

    def do_verify(self, ctx: HookContext, s: Session) -> OperationResult:
        c, svc = self.cfg(ctx), service_name(ctx)
        if not s.container_id(svc):
            raise HookFailure("not_provisioned", f"{ctx.resource_id} has not been provisioned")
        host = c.get("hostname")
        if self.tls(ctx):
            target = f"https://{host}{c['verifyPath']}"
            route = ["--connect-to", f"{host}:443:{svc}:443"]
        else:
            target = f"http://{host or svc}{c['verifyPath']}"
            route = ["--connect-to", f"{host}:80:{svc}:80"] if host else []
        ca = b""
        if self.tls(ctx) and c["tls"] == "internal":
            for _ in range(30):
                proc = s.run(["docker", "exec", s.container_id(svc), "cat", "/data/caddy/pki/authorities/local/root.crt"],
                             check=False)
                if proc.returncode == 0:
                    ca = proc.stdout
                    break
                time.sleep(1)
        script = ('cat > /tmp/ca.pem; set -- "$@"; '
                  'exec curl -sS -o /dev/null -D - --max-time 10 "$@"')
        args = route + ((["--cacert", "/tmp/ca.pem"]) if ca else []) + [target]
        status, header, detail = 0, False, ""
        deadline = time.monotonic() + 15  # a fresh Caddy or app may need a moment; a dead app gets 502 fast
        while True:
            proc = s.probe(CURL, ["-c", script, "verify"] + args, entrypoint="sh", stdin=ca, check=False,
                           what="requesting the app through caddy")
            text = s.text(proc)
            m = re.search(r"^HTTP/\S+ (\d{3})", text, re.M)
            status = int(m.group(1)) if m else 0
            header = bool(re.search(r"^x-boss-route:\s*" + re.escape(ctx.resource.name), text, re.M | re.I))
            detail = s.scrub(proc.stderr.decode(errors="replace").strip())[-200:]
            if (status and status not in (502, 503, 504)) or time.monotonic() > deadline:
                break
            time.sleep(1.5)
        reached = bool(status) and status not in (502, 503, 504) and header
        scheme = "HTTPS (certificate chain verified)" if self.tls(ctx) else "HTTP"
        ev = [evidence("request_through_caddy", reached, "connection",
                       f"{scheme} {target} -> {status or 'no answer'} from {c['upstream']}"
                       + ("" if header else "; route header missing") + (f"; {detail}" if not status else "")),
              self.published(ctx, s)]
        if reached and ev[1]["passed"]:
            return ctx.result(evidence=ev, state=ctx.state)
        return ctx.result(OperationStatus.FAILED, evidence=ev,
                          errors=[{"code": "verify_failed", "message": f"the app did not answer through caddy ({status})"}])

    def published(self, ctx: HookContext, s: Session) -> dict:
        c = self.cfg(ctx)
        proc = s.run(["docker", "port", s.container_id(service_name(ctx))], check=False)
        got = s.text(proc)
        want = [f"{c['bindAddress']}:{c['httpPort']}"] + ([f"{c['bindAddress']}:{c['httpsPort']}"] if self.tls(ctx) else [])
        ok = all(w in got for w in want)
        return evidence("ports_as_planned", ok, "config", f"published {', '.join(want)}" if ok else f"docker port: {got[:200]}")

    def do_destroy(self, ctx: HookContext, s: Session) -> OperationResult:
        self.guard_destroy(ctx)
        removed = s.compose_destroy(service_name(ctx), [self.volume(ctx)])
        return ctx.result(evidence=[evidence("removed", True, "cleanup", ", ".join(removed))], state={})


Plugin = CaddyPlugin
