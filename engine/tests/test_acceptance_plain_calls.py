"""PLAIN CALLS: the allow-list of commands whose output can be evidence (PR #154 review E1).

The probes that beat a block-list: `python3 -m timeit -n1 -r1 "open('.env','w').write('ON')"`,
`python3 -m timeit "print('FEATURE WORKS NOW')"`, `npm exec -c "echo …"`, `npx -c`, `make --eval`,
`mvn exec:exec`, -m pdb/code/cProfile. None of them is a plain call into the delivered work.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from boss_engine.services.acceptance.calls import Tree, plain_call

TRACKED = {
    "check.py": "print('ok')\n",
    "app/__init__.py": "",
    "app/__main__.py": "print('app')\n",
    "run.sh": "#!/bin/sh\necho run\n",
    "test/app.test.js": "",
    "server.js": "",
    "Makefile": "CC = cc\nOUT := bin\ncheck:\n\t@echo ok\nbuild test: deps\n\t@true\n",
    "package.json": json.dumps({"scripts": {"test": "node --test", "lint": "eslint ."}}),
    "go.mod": "module x\n",
    "cmd/gw/main.go": "package main\n",
    "pom.xml": "<project/>",
    "build.gradle": "",
    "Cargo.toml": "[package]\n",
}


@pytest.fixture(scope="module")
def tree(tmp_path_factory) -> Tree:
    root = tmp_path_factory.mktemp("tree")
    for name, body in TRACKED.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(body)
    (root / ".env").write_text("planted\n")                          # NOT tracked
    return Tree(root, TRACKED)


@pytest.mark.parametrize("cmd", [
    "python3 check.py", "python3 -u -W ignore check.py --flag -c x", "python3 -m pytest -q -p x",
    "python3 -m unittest discover", "python3 -m app", "python3 -m app.__main__", "pytest -q",
    "node server.js", "node --test test/app.test.js", "bash run.sh", "make", "make check",
    "make -s -j4 build test", "npm test", "npm run lint", "npm ci", "yarn lint", "pnpm run test",
    "go test ./...", "go vet ./cmd/...", "go build -o bin/gw ./cmd/gw", "go test -run=TestX -v .",
    "mvn -q test", "./mvnw verify", "gradle test", "./gradlew -q build", "cargo test --release",
])
def test_plain_calls_into_the_delivered_work(tree, cmd):
    assert plain_call(cmd.split(), tree) is None, cmd


@pytest.mark.parametrize("argv", [
    ["python3", "-m", "timeit", "-n1", "-r1", "open('.env','w').write('ON')"],
    ["python3", "-m", "timeit", "print('FEATURE WORKS NOW')"],
    ["python3", "-m", "pdb", "check.py"], ["python3", "-m", "code"],
    ["python3", "-m", "cProfile", "check.py"], ["python3", "-c", "print(1)"],
    ["python3", ".env"], ["python3", "missing.py"], ["python3"], ["python3", "-"],
    ["npm", "exec", "-c", "echo FEATURE ON"], ["npx", "-c", "echo hi"],
    ["npm", "run", "nope"], ["npm", "run", "lint", "--call", "x"], ["npm", "install", "left-pad"],
    ["make", "--eval", "x:;@echo hi", "x"], ["make", "-E", "x:;@echo hi"],
    ["make", "CC=./evil", "check"], ["make", "-f", "other.mk"], ["make", "-C", "/", "check"],
    ["make", "nope"], ["mvn", "exec:exec"], ["mvn", "-Dexec.args=x", "test"],
    ["gradle", "test", "--init-script", "x.gradle"], ["gradle", "run", "--args=x"],
    ["go", "run", "-ldflags", "-X main.msg=ON", "./cmd/gw"], ["go", "generate", "./..."],
    ["go", "test", "/etc"], ["cargo", "test", "--config", "x"], ["bash", "-c", "echo"],
    ["node", "-e", "1"], ["node", "--require", "./.env", "server.js"], ["deno", "eval", "1"],
    ["echo", "FEATURE ON"], ["perl", "check.py"],
])
def test_everything_else_is_not_a_plain_call(tree, argv):
    assert plain_call(argv, tree), argv


def test_a_call_from_a_subdirectory_resolves_its_paths(tree):
    sub = Tree(tree.root, TRACKED, "app")
    assert plain_call(["python3", "__main__.py"], sub) is None
    assert plain_call(["python3", "../check.py"], sub) is None
    assert plain_call(["python3", "../../etc/x.py"], sub)
