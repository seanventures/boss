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
  * static         a BUILD property of the NAMED binaries, proven only by a command BOSS
                   recorded and verified (`ldd` / `file` of one of them, `go build` with
                   CGO_ENABLED=0 of one of them) or by the Dockerfile that builds them: an
                   explicit CGO_ENABLED=0 in the stage the final stage copies the binary from,
                   or a final stage FROM scratch / distroless static copying exactly that
                   binary. Otherwise a person confirms it (HUMAN) — never met, never a gap
                   (PR #177 review: no inference from a compiler-less alpine image);
  * dependencies   as acceptance/dependencies.claim reads them;
  * anything else  a part BOSS cannot check, named.
The item is met only when every part is proven. Northgate's re-check therefore reads 29 met and
item 6 HUMAN. A container path is mapped back to the tree through a Dockerfile COPY (relative to
its WORKDIR, never `--from` another stage) or the bind mounts of the compose service that sets it;
services that disagree resolve nothing.

The fixture is the re-check's real row (each item and each answer as the reviewer gave it) over
the Northgate tree of acceptance_ar29.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typing import Optional

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


def test_the_r8b_recheck_replays_29_met_and_the_stack_line_needs_a_person():
    assert R8B["recorded"] == {"met": 29, "missing": 0, "partial": 1, "unjudged": 0}
    assert [a["n"] for a in R8B["answers"] if a.get("recorded_reason")] == [6]
    rec = ag.judge(R8B["items"], _reply(R8B["answers"]), {}, root=TREE)
    assert rec["counts"]["met"] == 29 and not rec["partial"] and not rec["missing"], [
        (i["n"], i["evidence"][-160:]) for i in rec["items"] if i["status"] != "met"]
    six = next(i for i in rec["items"] if i["n"] == 6)
    assert six["status"] == "human" and "static binaries: not proven" in six["evidence"]
    assert [i["n"] for i in rec["items"] if i["status"] == "human"] == [6]
    assert rec["verdict"] == "not_met"           # a person still confirms it; never a pass
    from boss_engine.services.acceptance import ledger

    assert ledger._gap_of(six) == "minor"         # never a major gap


def test_the_northgate_stack_item_needs_a_person_not_a_pass():
    got = deps.stack_claim(STACK, TREE)
    assert got[0] == "human", got
    assert "no package named exactly gateway" in got[1]
    assert "static binaries: not proven by a recorded check" in got[1]


# ── a tree to vary ──────────────────────────────────────────────────────────────────────────

NAMED = "Stack: Go. (northgate-gateway and gwlint: one Go module, static binaries)"


def _tree(tmp_path, dockerfile: str = "", *, mods=("",), cmds=("northgate-gateway", "gwlint"),
          extra: Optional[dict] = None):
    for m in mods:
        (tmp_path / m).mkdir(parents=True, exist_ok=True)
        (tmp_path / m / "go.mod").write_text("module northgate\n\ngo 1.23\n")
    for c in cmds:
        (tmp_path / "cmd" / c).mkdir(parents=True, exist_ok=True)
        (tmp_path / "cmd" / c / "main.go").write_text("package main\n\nfunc main() {}\n")
    if dockerfile:
        (tmp_path / "Dockerfile").write_text(dockerfile)
    for name, text in (extra or {}).items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    return tmp_path


SHIP = ("FROM debian:bookworm-slim\nCOPY --from=build /out/northgate-gateway /usr/local/bin/\n"
        "COPY --from=build /out/gwlint /usr/local/bin/\n")


@pytest.mark.parametrize("dockerfile", [
    "FROM golang:1.23 AS build\n"
    "RUN CGO_ENABLED=0 go build -o /out/northgate-gateway ./cmd/northgate-gateway\n"
    "RUN CGO_ENABLED=0 go build -o /out/gwlint ./cmd/gwlint\n" + SHIP,
    "FROM golang:1.23 AS build\nENV CGO_ENABLED=0\n"
    "RUN go build -o /out/northgate-gateway ./cmd/northgate-gateway && \\\n"
    "    go build -o /out/gwlint ./cmd/gwlint\n" + SHIP,
    "FROM golang:1.23 AS build\n"
    "RUN export CGO_ENABLED=0; go build -o /out/northgate-gateway ./cmd/northgate-gateway; "
    "go build -o /out/gwlint ./cmd/gwlint\n" + SHIP,
])
def test_an_explicit_cgo_off_build_of_the_named_binaries_proves_static(tmp_path, dockerfile):
    got = deps.stack_claim(NAMED, _tree(tmp_path, dockerfile))
    assert got[0] == "met", got


def test_a_scratch_image_copying_exactly_the_binary_proves_static(tmp_path):
    item = "Stack: Go. (gwlint: one Go module, static binaries)"
    for base in ("scratch", "gcr.io/distroless/static-debian12"):
        root = _tree(tmp_path, "FROM golang:1.23 AS build\nRUN go build -o /out/gwlint ./cmd/gwlint\n"
                               f"FROM {base}\nCOPY --from=build /out/gwlint /gwlint\n")
        assert deps.stack_claim(item, root)[0] == "met", base


GW = "RUN go build -o /out/northgate-gateway ./cmd/northgate-gateway\nRUN go build -o /out/gwlint ./cmd/gwlint\n"


@pytest.mark.parametrize("dockerfile,extra", [
    # the review's probes: none of these is a static build of the named binaries
    ("FROM golang:1.23-alpine AS build\nRUN apk add --no-cache \\\n    gcc musl-dev\n" + GW + SHIP, None),
    ("FROM golang:1.23-alpine AS build\nRUN apk add g++\n" + GW + SHIP, None),
    ("FROM golang:1.23-alpine AS build\nRUN apk add alpine-sdk\n" + GW + SHIP, None),
    ("FROM golang:1.23-alpine AS build\n" + GW + SHIP, None),       # no inference from alpine
    ("FROM golang:1.23 AS build\nRUN make build\n" + SHIP, None),   # built by make
    ("FROM golang:1.23 AS build\n" + GW + SHIP,                        # another Dockerfile
     {"mocks/echo/Dockerfile": "FROM golang:1.23 AS b\nRUN CGO_ENABLED=0 go build -o /e ./mocks/echo\n"
                               "FROM scratch\nCOPY --from=b /e /e\n"}),
    ("FROM golang:1.23 AS build\nRUN CGO_ENABLED=0 go build -o /out/echo ./mocks/echo-upstream\n"
     + GW + SHIP, None),                                              # cgo off for another package
    ("FROM golang:1.23 AS tools\nRUN CGO_ENABLED=0 go build -o /out/gwlint ./cmd/gwlint\n"
     "FROM golang:1.23 AS build\n" + GW + SHIP, None),               # not the stage it ships
    ("FROM golang:1.23 AS build\n" + GW + "FROM scratch\nCOPY --from=build /out/ /bin/\n", None),
    ("", None),
])
def test_nothing_but_a_check_of_the_named_binaries_proves_static(tmp_path, dockerfile, extra):
    root = _tree(tmp_path, dockerfile, extra=extra)
    got = v.parse_acceptance(
        "ITEM 1: MET — single go.mod; built binary confirmed statically linked "
        "(`ldd` → not a dynamic executable)\n", [{"n": 1, "text": NAMED}], root=root)[0]
    assert got["status"] == "human", got
    assert "static binaries: not proven by a recorded check" in got["evidence"]


@pytest.mark.parametrize("text,mods,cmds,extra,status,why", [
    (NAMED, ("", "tools"), ("northgate-gateway", "gwlint"), None, "false", "2 go.mod files"),
    ("Stack: Go. (northgate-gateway and billing: one Go module)", ("",),
     ("northgate-gateway", "gwlint"), None, "false", "no package for billing"),
    ("Stack: Go. (gateway and gwlint: one Go module)", ("",), ("northgate-gateway", "gwlint"),
     None, "human", "no package named exactly gateway"),
    ("Stack: Go. (gwlint and docs: one Go module)", ("",), ("gwlint",),
     {"cmd/docs/README.md": "no go here\n"}, "false", "no package for docs"),   # no .go files
    ("Stack: Go. (gwlint: one Go module)", ("",), ("gwlint",),
     {"go.work": "go 1.23\n\nuse (\n\t.\n\t./tools\n)\n"}, "human", "go.work uses 2"),
    ("Stack: Rust.", ("",), ("gwlint",), None, "false", "no rust in the tree"),
    ("Stack: Go. (one Go module, deployed on Kubernetes)", ("",), ("gwlint",), None, "human",
     "cannot check"),
    ("Stack: Elixir.", ("",), ("gwlint",), None, "human", "cannot be checked"),
])
def test_every_part_must_hold(tmp_path, text, mods, cmds, extra, status, why):
    got = deps.stack_claim(text, _tree(tmp_path, mods=mods, cmds=cmds, extra=extra))
    assert got[0] == status and why in got[1], got


def test_a_non_stack_item_is_not_a_stack_item():
    assert deps.stack_claim("A quote rounds half-even", TREE) is None
    assert deps.stack_claim("Written in Go", TREE) is None


# ── the exercised door: a command BOSS recorded and verified, of a NAMED binary ─────────────

def _record(*cmds: Command) -> CommandRecord:
    r = CommandRecord()
    for c in cmds:
        r.add(c)
    return r


def _run(tmp_path, reply: str, *cmds: Command):
    item = "Stack: Go. (gwlint: one Go module, static binaries)"
    return v.parse_exercised(reply, [{"n": 1, "id": "AC-1", "text": item}],
                             root=_tree(tmp_path), exec_log=_record(*cmds))[0]


def test_a_recorded_ldd_of_the_named_binary_proves_static(tmp_path):
    row = _run(tmp_path, 'AC-1: MET — E1 (exit 1) "not a dynamic executable"\n',
               Command(id="E1", cmd="ldd /out/gwlint", argv=["ldd", "/out/gwlint"], exit=1,
                       stdout="\tnot a dynamic executable\n", tree_dirty=False, plain=True))
    assert row["status"] == "met", row


def test_a_recorded_cgo_off_go_build_of_the_named_package_proves_static(tmp_path):
    row = _run(tmp_path, 'AC-1: MET — E1 (exit 0) "built gwlint ok"\n',
               Command(id="E1", cmd="CGO_ENABLED=0 go build -o /tmp/gwlint ./cmd/gwlint",
                       exit=0, stdout="built gwlint ok\n", tree_dirty=False, plain=True))
    assert row["status"] == "met", row


@pytest.mark.parametrize("cmd,out", [
    (Command(id="E1", cmd="file /bin/busybox", argv=["file", "/bin/busybox"], exit=0,
             stdout="/bin/busybox: ELF 64-bit, statically linked, stripped\n",
             tree_dirty=False, plain=True), "statically linked"),          # not a named binary
    (Command(id="E1", cmd="CGO_ENABLED=0 go build -o /tmp/echo ./mocks/echo-upstream", exit=0,
             stdout="built echo ok\n", tree_dirty=False, plain=True), "built echo ok"),
    (Command(id="E1", cmd="ldd /out/gwlint", argv=["ldd", "/out/gwlint"], exit=0,
             stdout="\tlinux-vdso.so.1 (0x0000)\n\tlibc.so.6 => /lib/libc.so.6\n",
             tree_dirty=False, plain=True), "libc.so.6"),                  # dynamic after all
])
def test_a_recorded_check_of_anything_else_proves_nothing(tmp_path, cmd, out):
    row = _run(tmp_path, f'AC-1: MET — E1 (exit {cmd.exit}) "{out}"\n', cmd)
    assert row["status"] == "human" and "static binaries: not proven" in row["evidence"], row


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


def test_each_service_maps_through_its_own_mounts_and_disagreeing_ones_resolve_nothing(tmp_path):
    d = _env_tree(tmp_path, **{"docker-compose.yml": (
        "services:\n  app:\n    environment:\n      - CONFIG_DIR=/app/config\n"
        "  sidecar:\n    volumes:\n      - ./config:/app/config\n")})
    # the mount is another service's: the app's /app/config is not the tree's config
    assert d.param_value("load.go", "load", "dir") == "/app/config"
    (tmp_path / "other").mkdir()
    d = _env_tree(tmp_path / "x", **{"docker-compose.yml": (
        "services:\n  a:\n    environment:\n      CONFIG_DIR: /app/c\n    volumes:\n"
        "      - ./config:/app/c\n  b:\n    environment:\n      CONFIG_DIR: /app/c\n"
        "    volumes:\n      - ./other:/app/c\n")})
    assert d.param_value("load.go", "load", "dir") is None


def test_a_copy_from_another_stage_is_never_the_tree(tmp_path):
    d = _env_tree(tmp_path, Dockerfile="FROM alpine\nWORKDIR /app\n"
                                       "COPY --chown=1000 --from=build /src/config ./config\n"
                                       "ENV CONFIG_DIR=/app/config\n")
    assert d.param_value("load.go", "load", "dir") == "/app/config"
