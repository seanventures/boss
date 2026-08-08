"""Setting one thing on a target must not delete another.

`required_resources` is a single JSONB array holding unrelated facts — container runtime, port,
which app in the repo to deploy, who owns it, which ports the host publishes. Three separate
places wrote it by REPLACING the whole array with `[container_runtime, app_port]`, so choosing a
port silently deleted `app_entry`. The next deployment then failed with "cannot tell which app
to deploy — candidates: …8 runnable apps", naming a field nobody had touched.

I fixed two of those three writers and missed the third, and the bug came straight back. That is
what this file is for: the merge is one function, and these tests pin its behaviour.

    PYTHONPATH=engine .venv/bin/python tests/test_target_resources.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "engine")
sys.path.insert(0, ".")

from apps.api.app.main import merge_target_resources  # noqa: E402
from boss_engine.services.deployment_connectors import (  # noqa: E402
    _app_entry, _app_port, _publishable_ports, _render_pool, domain_with_port,
)

_failures: list[str] = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  PASS  {name}")
        return
    _failures.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  FAIL  {name}\n        got  {got!r}\n        want {want!r}")


def kinds(resources) -> dict:
    return {r["kind"]: r["name"] for r in resources}


def main() -> int:
    print("== merging resources ==")
    full = [
        {"kind": "container_runtime", "name": "docker"},
        {"kind": "app_entry", "name": "web-nqueens"},
        {"kind": "publishable_ports", "name": "3003,3005"},
        {"kind": "app_port", "name": "3010"},
    ]

    # THE bug: a port change wiped app_entry, and the deployment that followed could not tell
    # which of eight apps in the repo it was supposed to ship.
    after = kinds(merge_target_resources(full, {"app_port": "3005"}))
    check("changing the port keeps app_entry", after.get("app_entry"), "web-nqueens")
    check("changing the port keeps the port pool",
          after.get("publishable_ports"), "3003,3005")
    check("changing the port changes the port", after.get("app_port"), "3005")

    check("an empty value clears just that kind",
          "app_port" in kinds(merge_target_resources(full, {"app_port": ""})), False)
    check("clearing the port keeps everything else",
          kinds(merge_target_resources(full, {"app_port": ""})).get("app_entry"),
          "web-nqueens")
    check("None means 'not mentioned', not 'delete'",
          kinds(merge_target_resources(full, {"app_entry": None})).get("app_entry"),
          "web-nqueens")
    check("the container runtime is always present",
          kinds(merge_target_resources([], {"app_port": "80"})).get("container_runtime"),
          "docker")
    check("a new kind is added, not swapped in",
          len(merge_target_resources(full, {"repo_owner": "paramg"})), 5)
    check("junk entries are dropped rather than crashing",
          kinds(merge_target_resources([None, "nonsense", *full], {"app_port": "1"}))
          .get("app_entry"), "web-nqueens")

    print("== reading resources back ==")
    target = {"required_resources": merge_target_resources(full, {"app_port": "3005"})}
    check("app_entry reads back", _app_entry(target), "web-nqueens")
    check("app_port reads back", _app_port(target), 3005)
    check("the pool reads back", _publishable_ports(target), [3003, 3005])

    print("== the port pool ==")
    # A free port behind a closed firewall passes every local check and then times out at the
    # health check with nothing naming the security group. This is how BOSS avoids picking one.
    check("ranges expand", _publishable_ports(
        {"required_resources": [{"kind": "publishable_ports", "name": "10000-10002"}]}),
        [10000, 10001, 10002])
    check("whitespace and mixed forms", _publishable_ports(
        {"required_resources": [{"kind": "publishable_ports", "name": " 3003 , 4000-4001 "}]}),
        [3003, 4000, 4001])
    check("no pool declared means no constraint",
          _publishable_ports({"required_resources": []}), None)
    check("junk is not a pool", _publishable_ports(
        {"required_resources": [{"kind": "publishable_ports", "name": "nonsense"}]}), None)
    # An unbounded range would let a typo allocate half a million integers.
    check("a huge range is capped", len(_publishable_ports(
        {"required_resources": [{"kind": "publishable_ports", "name": "1-99999"}]})), 501)
    check("the pool prints abbreviated", _render_pool(list(range(1, 20))),
          "1, 2, 3, 4, 5, 6, 7, 8…")

    print("== the URL must name the port ==")
    # A domain of "3.94.66.139" is port 80, and on that host port 80 is an unrelated product
    # answering 200 — so an unported domain is a false green waiting to happen, not untidiness.
    check("an unported domain gains the port",
          domain_with_port("3.94.66.139", "3.94.66.139", 3005), "3.94.66.139:3005")
    check("an empty domain falls back to the ssh host",
          domain_with_port(None, "3.94.66.139", 3005), "3.94.66.139:3005")
    check("a domain that already names a port is left alone",
          domain_with_port("3.94.66.139:3004", "3.94.66.139", 3005), None)
    check("nothing to work with means no change",
          domain_with_port("", "", 3005), None)
    check("hostnames work the same way",
          domain_with_port("app.example.com", "h", 8080), "app.example.com:8080")

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("all target-resource checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
