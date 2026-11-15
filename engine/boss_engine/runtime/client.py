"""The run-plane client: what boss-runtime calls instead of a database (boss-agent-hosts-lld.md
§3.13.3).

In a microVM the runtime talks to `127.0.0.1:71xx`, which boss-vsockd bridges over vsock to
boss-hostd; boss-hostd identifies the VM by its socket, attaches the host's credential and
forwards to the control plane. The guest holds no credential (G4). On the `process` backend (a
laptop, CI) the same client talks to the control plane directly and signs as the host it stands
in for (`HostSigner`): the transport changes, the calls do not.

Every run-scoped call carries the run's lease: X-Boss-Replica, X-Boss-Job, X-Boss-Run-Token and
X-Boss-Fence-Epoch. A 409 `fenced` answer raises `Fenced`: the run is someone else's now, and the
runtime abandons it without writing anything more.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

PREFIX = "/v1/run-plane"


class RunPlaneHTTPError(RuntimeError):
    def __init__(self, status: int, body: Any):
        detail = body.get("detail", body) if isinstance(body, dict) else body
        code = detail.get("error") if isinstance(detail, dict) else None
        super().__init__(f"run plane answered {status}: {code or detail}")
        self.status, self.body, self.code = status, detail, code


class Fenced(RunPlaneHTTPError):
    """409 fenced: this executor's lease is stale (§2.14.2)."""


class HostSigner:
    """Signs each call as the host (a `BossHost` proof, services/fleet_ca.py). Used only where
    no boss-hostd sits between the runtime and the control plane (the process backend)."""

    def __init__(self, private_key: Any, cert_pem: str):
        self.private_key, self.cert_pem = private_key, cert_pem

    def __call__(self, method: str, path: str) -> dict:
        from ..services.fleet_ca import sign_proof

        return {"Authorization": "BossHost " + sign_proof(self.private_key, self.cert_pem,
                                                         htm=method, path=path)}


class HttpTransport:
    """JSON over HTTP. `client` is an httpx.Client (or a Starlette TestClient); `signer`
    (method, path) -> headers."""

    def __init__(self, client: Any, *, signer: Optional[Callable[[str, str], dict]] = None,
                 base_path: str = ""):
        self.client, self.signer, self.base_path = client, signer, base_path.rstrip("/")
        self._lock = threading.Lock()

    def request(self, method: str, path: str, *, json: Any = None, params: Optional[dict] = None,
                headers: Optional[dict] = None) -> Any:
        full = self.base_path + path
        sent = dict(headers or {})
        if self.signer is not None:
            sent.update(self.signer(method, full))
        response = self.client.request(method, full, json=json, params=params, headers=sent)
        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text[:500]}
        if response.status_code >= 400:
            detail = body.get("detail", body) if isinstance(body, dict) else body
            if response.status_code == 409 and isinstance(detail, dict) \
                    and detail.get("error") == "fenced":
                raise Fenced(response.status_code, body)
            raise RunPlaneHTTPError(response.status_code, body)
        return body


@dataclass(frozen=True)
class Lease:
    replica_id: str
    job_id: str
    run_token: str
    fence_epoch: int

    def headers(self) -> dict:
        out = {"X-Boss-Replica": self.replica_id, "X-Boss-Job": self.job_id,
               "X-Boss-Fence-Epoch": str(self.fence_epoch)}
        if self.run_token:  # in a VM the host attaches it; the guest never holds one
            out["X-Boss-Run-Token"] = self.run_token
        return out

    def __repr__(self) -> str:  # never print the run token
        return f"Lease(replica={self.replica_id}, job={self.job_id}, epoch={self.fence_epoch})"


class HostWorkClient:
    """boss-hostd's ONE work request for the whole host (§3.13.0), in Python: the fake host of
    the tests and the reference for the Go side (docs/infra/HOST_API.md §2.8)."""

    def __init__(self, transport: "HttpTransport"):
        self.transport = transport

    def next(self, *, state_gen: int, wait_ms: int = 0, ttl: Optional[int] = None) -> dict:
        """{"runs": [...]} or {"resync": true}. The body is never a replica list."""
        return self.transport.request("POST", f"{PREFIX}/hosts/next", json={
            "wait_ms": wait_ms, "state_gen": state_gen, "ttl": ttl})

    def put_replicas(self, host_id: str, *, state_gen: int, replicas: list) -> dict:
        return self.transport.request("PUT", f"/v1/hosts/{host_id}/replicas", json={
            "state_gen": state_gen, "replicas": replicas})


class RunPlaneClient:
    def __init__(self, transport: HttpTransport, replica_id: str):
        self.transport, self.replica_id = transport, replica_id

    def _replica(self) -> dict:
        # In a VM, boss-hostd's guest gateway overwrites this from the socket the call came in
        # on; on the process backend the runtime stands in for the host and says it itself.
        return {"X-Boss-Replica": self.replica_id}

    def admit(self, *, caller: str, input: Any, thread_id: Optional[str] = None,
              double_text: Optional[str] = None, ttl: Optional[int] = None) -> dict:
        return self.transport.request("POST", f"{PREFIX}/admit", json={
            "caller": caller, "input": input, "thread_id": thread_id,
            "double_text": double_text, "ttl": ttl}, headers=self._replica())

    def assigned(self, work: dict) -> "RunHandle":
        """The handle for a run the host handed this VM over the work inbox: {run_id, job_id,
        replica_id, epoch}. No run token: the host attaches it to every call."""
        return RunHandle(self.transport, str(work["run_id"]),
                         Lease(str(work.get("replica_id") or self.replica_id),
                               str(work["job_id"]), str(work.get("run_token") or ""),
                               int(work["epoch"])))

    def run(self, package: dict) -> "RunHandle":
        lease = package["lease"]
        return RunHandle(self.transport, str(package["run"]["id"]),
                         Lease(str(lease["replica_id"]), str(lease["job_id"]),
                               str(lease.get("run_token") or ""), int(lease["fence_epoch"])))


class RunHandle:
    """One leased run's calls."""

    def __init__(self, transport: HttpTransport, run_id: str, lease: Lease):
        self.transport, self.run_id, self.lease = transport, run_id, lease
        self.stop_flag = False

    def _path(self, tail: str) -> str:
        return f"{PREFIX}/runs/{self.run_id}/{tail}"

    def call(self, method: str, tail: str, *, json: Any = None,
             params: Optional[dict] = None) -> Any:
        return self.transport.request(method, self._path(tail), json=json, params=params,
                                      headers=self.lease.headers())

    # the lease
    def heartbeat(self, ttl: Optional[int] = None) -> dict:
        out = self.call("POST", "lease", json={"action": "heartbeat", "ttl": ttl})
        self.stop_flag = bool(out.get("stop"))
        return out

    def should_stop(self) -> bool:
        return bool(self.heartbeat().get("stop"))

    def settle(self, outcome: dict) -> dict:
        return self.call("POST", "lease", json={"action": "settle", "outcome": outcome})

    def fail(self, error: str) -> dict:
        return self.call("POST", "lease", json={"action": "fail", "error": error})

    def package(self) -> dict:
        """The run this VM was handed: what it drives the kernel from (GET /runs/{run})."""
        return self.transport.request("GET", f"{PREFIX}/runs/{self.run_id}",
                                      headers=self.lease.headers())

    def hand_back(self) -> dict:
        return self.call("POST", "lease", json={"hand_back": True})

    def answers(self) -> dict:
        return self.call("GET", "interrupts/answers").get("answers") or {}


__all__ = ["Fenced", "HostSigner", "HostWorkClient", "HttpTransport", "Lease", "PREFIX", "RunHandle",
           "RunPlaneClient", "RunPlaneHTTPError"]
