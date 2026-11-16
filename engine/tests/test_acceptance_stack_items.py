"""A `Stack:` acceptance item is proven part by part (Northgate r8b, the re-check
crun_579975fa542645f0a9d5).

The re-check was 29 met / 1 partial. The partial, item 6 "Stack: Go. (gateway and gwlint: one Go
module, static binaries)", was marked "only manifests/lockfiles cited". The reviewer cited the one
go.mod and an `ldd` it ran, which BOSS did not record. Replayed against main after #172, it was
28/2: the compose file sets CONFIG_DIR=/app/config-staging over a `./config` bind mount, and that
container path was no longer the tree's `config`, so item 2's loader chain broke too.

Now:
  * language       the tree has that language's manifest or code;
  * one Go module  exactly one go.mod, with every package the item names under it;
  * static         a BUILD property, proven by a command BOSS recorded and verified (`ldd` /
                   `file` / `go build` with CGO_ENABLED=0) or by a Dockerfile build that makes the
                   binaries static (CGO_ENABLED=0; a Go 1.20+ golang alpine stage with no C
                   compiler installed; a final stage FROM scratch or distroless/static); never
                   by the answer's prose;
  * dependencies   as acceptance/dependencies.claim reads them;
  * anything else  a part BOSS cannot check, named.
The item is met only when every part is proven. A container path is mapped back to the tree
through a Dockerfile COPY (relative to its WORKDIR) or a compose bind mount.

The fixture is the re-check's real row (each item and each answer as the reviewer gave it) over
the Northgate tree of acceptance_ar29.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from boss_engine.services import acceptance_gate as ag
from boss_engine.services.acceptance import dependencies as deps
from boss_engine.services.acceptance import verdict as v
from boss_engine.services.acceptance.session import Command, CommandRecord

FIX = Path(__file__).parent / "fixtures" / "acceptance_ar29"
R8B = json.loads((FIX / "r8b_check.json").read_text(encoding="utf-8"))
TREE = FIX / "tree"
STACK = "Stack: Go. (gateway and gwlint: one Go module, static binaries)"


def _reply(answers) -> str:
    return "\n".join(f"ITEM {a['n']}: {a['said']} — {a['evidence']}" for a in answers) \
        + "\nACCEPTANCE: MET\n"


def test_the_r8b_recheck_replays_30_of_30():
    assert R8B["recorded"] == {"met": 29, "missing": 0, "partial": 1, "unjudged": 0}
    assert [a["n"] for a in R8B["answers"] if a.get("recorded_reason")] == [6]
    rec = ag.judge(R8B["items"], _reply(R8B["answers"]), {}, root=TREE)
    assert rec["counts"]["met"] == 30 and rec["verdict"] == "met", [
        (i["n"], i["evidence"][-160:]) for i in rec["items"] if i["status"] != "met"]


def test_the_northgate_stack_item_is_proven_part_by_part():
    got = deps.stack_claim(STACK, TREE)
    assert got[0] == "met", got
    assert "one Go module (gateway, gwlint)" in got[1] and "static: Dockerfile" in got[1]


# ── a tree to vary ──────────────────────────────────────────────────────────────────────────

def _tree(tmp_path, dockerfile: str, *, mods=("",), cmds=("northgate-gateway", "gwlint")):
    for m in mods:
        (tmp_path / m).mkdir(parents=True, exist_ok=True)
        (tmp_path / m / "go.mod").write_text("module northgate\n\ngo 1.23\n")
    for c in cmds:
        (tmp_path / "cmd" / c).mkdir(parents=True, exist_ok=True)
        (tmp_path / "cmd" / c / "main.go").write_text("package main\n\nfunc main() {}\n")
    if dockerfile:
        (tmp_path / "Dockerfile").write_text(dockerfile)
    return tmp_path


PLAIN = ("FROM golang:1.23 AS build\nWORKDIR /src\nCOPY . .\n"
         "RUN go build -o /out/gw ./cmd/northgate-gateway\nFROM debian:bookworm-slim\n"
         "COPY --from=build /out/gw /gw\n")


@pytest.mark.parametrize("dockerfile", [
    "FROM golang:1.23 AS build\nRUN CGO_ENABLED=0 go build -o /out/gw ./cmd/northgate-gateway\n"
    "FROM debian:bookworm-slim\n",
    "FROM golang:1.23 AS build\nENV CGO_ENABLED=0\nRUN go build -o /out/gw ./cmd/northgate-gateway\n"
    "FROM debian:bookworm-slim\n",
    "FROM golang:1.23-alpine AS build\nRUN go build -o /out/gw ./cmd/northgate-gateway\n"
    "FROM alpine:3.20\n",
    PLAIN + "FROM scratch\nCOPY --from=build /out/gw /gw\n",
    PLAIN + "FROM gcr.io/distroless/static-debian12\nCOPY --from=build /out/gw /gw\n",
])
def test_a_dockerfile_build_that_makes_static_binaries_proves_it(tmp_path, dockerfile):
    assert deps.stack_claim(STACK, _tree(tmp_path, dockerfile))[0] == "met"


@pytest.mark.parametrize("dockerfile", [
    PLAIN,                                                         # cgo on (a C compiler there)
    "FROM golang:1.23-alpine AS build\nRUN apk add --no-cache build-base\n"
    "RUN go build -o /out/gw ./cmd/northgate-gateway\nFROM alpine:3.20\n",
    "FROM golang:1.19-alpine AS build\nRUN go build -o /out/gw ./cmd/northgate-gateway\n"
    "FROM alpine:3.20\n",                                          # before Go 1.20's default
    "FROM golang:1.23-alpine AS build\nENV CGO_ENABLED=1\n"
    "RUN go build -o /out/gw ./cmd/northgate-gateway\nFROM alpine:3.20\n",
    "FROM golang:1.23 AS build\nRUN CGO_ENABLED=0 go build -o /out/gw ./cmd/northgate-gateway\n"
    "RUN go build -o /out/lint ./cmd/gwlint\nFROM debian:bookworm-slim\n",   # one of two
    "",                                                            # no Dockerfile at all
])
def test_prose_alone_never_proves_static_binaries(tmp_path, dockerfile):
    root = _tree(tmp_path, dockerfile)
    got = v.parse_acceptance(
        "ITEM 1: MET — single go.mod; built binary confirmed statically linked "
        "(`ldd` → not a dynamic executable)\n", [{"n": 1, "text": STACK}], root=root)[0]
    assert got["status"] == "partial" and "not proven: static binaries" in got["evidence"], got


@pytest.mark.parametrize("text,mods,cmds,why", [
    (STACK, ("", "tools"), ("northgate-gateway", "gwlint"), "2 go.mod files"),
    ("Stack: Go. (gateway and billing: one Go module, static binaries)", ("",),
     ("northgate-gateway", "gwlint"), "no package for billing"),
    ("Stack: Rust.", ("",), ("gwlint",), "no rust in the tree"),
    ("Stack: Go. (one Go module, deployed on Kubernetes)", ("",), ("gwlint",), "cannot check"),
    ("Stack: Elixir.", ("",), ("gwlint",), "cannot be checked"),
])
def test_every_part_must_hold(tmp_path, text, mods, cmds, why):
    got = deps.stack_claim(text, _tree(tmp_path, PLAIN.replace("go build", "CGO_ENABLED=0 go build"),
                                       mods=mods, cmds=cmds))
    assert got[0] == "false" and why in got[1], got


def test_a_non_stack_item_is_not_a_stack_item():
    assert deps.stack_claim("A quote rounds half-even", TREE) is None
    assert deps.stack_claim("Written in Go", TREE) is None


# ── the exercised door: a command BOSS recorded and verified ────────────────────────────────

def _record(stdout: str) -> CommandRecord:
    r = CommandRecord()
    r.add(Command(id="E1", cmd="ldd /out/northgate-gateway", argv=["ldd", "/out/northgate-gateway"],
                  exit=1, stdout=stdout, tree_dirty=False, plain=True))
    return r


def test_a_recorded_ldd_proves_static_binaries(tmp_path):
    root = _tree(tmp_path, PLAIN)
    reply = 'AC-1: MET — one go.mod; E1 (exit 1) "not a dynamic executable"\n'
    rows = v.parse_exercised(reply, [{"n": 1, "id": "AC-1", "text": STACK}], root=root,
                             exec_log=_record("\tnot a dynamic executable\n"))
    assert rows[0]["status"] == "met", rows


def test_a_quote_the_record_does_not_hold_proves_nothing(tmp_path):
    root = _tree(tmp_path, PLAIN)
    reply = 'AC-1: MET — one go.mod; E1 (exit 1) "not a dynamic executable"\n'
    rows = v.parse_exercised(reply, [{"n": 1, "id": "AC-1", "text": STACK}], root=root,
                             exec_log=_record("\tlinux-vdso.so.1 (0x0000)\n\tlibc.so.6\n"))
    assert rows[0]["status"] == "partial" and "not proven: static binaries" in rows[0]["evidence"]


# ── a container path is the tree path a COPY or a bind mount put there ─────────────────────

def _env_tree(tmp_path, **files):
    (tmp_path / "config" / "tenants").mkdir(parents=True)
    (tmp_path / "config" / "tenants" / "a.yaml").write_text("id: a\n")
    (tmp_path / "load.go").write_text(
        "package main\n\nimport (\n\t\"os\"\n\t\"path/filepath\"\n)\n\n"
        "func load(dir string) ([]byte, error) { return os.ReadFile(filepath.Join(dir, \"tenants\", \"a.yaml\")) }\n")
    (tmp_path / "main.go").write_text(
        "package main\n\nimport \"os\"\n\nfunc getenv(k, d string) string {\n"
        "\tif v := os.Getenv(k); v != \"\" {\n\t\treturn v\n\t}\n\treturn d\n}\n\n"
        "func main() {\n\tdir := getenv(\"CONFIG_DIR\", \"config\")\n\tload(dir)\n}\n")
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return v._Definitions(tmp_path)


def test_a_compose_bind_mount_maps_the_container_path_back(tmp_path):
    d = _env_tree(tmp_path, **{"docker-compose.yml": (
        "services:\n  staging:\n    environment:\n      - CONFIG_DIR=/app/config-staging\n"
        "    volumes:\n      - ./config:/app/config-staging:ro\n  prod:\n    environment:\n"
        "      - CONFIG_DIR=/app/config-production\n    volumes:\n"
        "      - ./config:/app/config-production:ro\n")})
    assert d.param_value("load.go", "load", "dir") == "config"
    assert d.body_reads("load.go", "load", ["config/tenants/a.yaml"])


def test_a_dockerfile_copy_under_its_workdir_maps_back(tmp_path):
    d = _env_tree(tmp_path, Dockerfile="FROM alpine\nWORKDIR /app\nCOPY config ./config\n"
                                       "ENV CONFIG_DIR=/app/config\n")
    assert d.param_value("load.go", "load", "dir") == "config"


def test_a_container_path_nothing_maps_stays_as_it_is(tmp_path):
    d = _env_tree(tmp_path, **{"docker-compose.yml": (
        "services:\n  app:\n    environment:\n      - CONFIG_DIR=/etc/northgate\n")})
    assert d.param_value("load.go", "load", "dir") == "/etc/northgate"
    assert not d.body_reads("load.go", "load", ["config/tenants/a.yaml"])
