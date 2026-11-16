"""Chat and Plan on the builder thread (BOSSAPP-1244).

Chat answers on the thread. Plan stores a plan. Neither creates a code file.
A build writes one code file, and only after a person approves that plan.
A repeat of the same approved build does not write again.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any, Optional

CODE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs",
    ".java", ".css", ".sql", ".html", ".vue", ".rb", ".php",
}
_MODES = ("chat", "plan", "build")
_THREAD_ID = re.compile(r"^bt_[a-f0-9]{16}$")
_PERSON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_NOT_A_PERSON = {"builder", "agent", "system", "boss", "assistant"}
_CODE_FILE = "src/approved_build.py"


class BuilderRefused(Exception):
    """A visible refusal. The workspace is unchanged."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def body(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "wrote_code": False,
            "code_files": [],
        }


def resolve_root(root: Optional[Path]) -> Path:
    """The directory that holds thread workspaces. Never a client-chosen path."""
    if root is not None:
        path = Path(root).expanduser().resolve()
    else:
        raw = os.environ.get("BOSS_BUILDER_THREAD_ROOT", "").strip()
        if not raw:
            raise BuilderRefused(
                "no_workspace", "Builder threads have no workspace yet.", status=503)
        path = Path(raw).expanduser().resolve()
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise BuilderRefused(
            "no_workspace", "Builder thread root is not a directory.", status=503)
    return path


def open_thread(root: Path, *, name: str) -> dict[str, Any]:
    label = _label(name)
    base = resolve_root(root)
    thread_id = "bt_" + secrets.token_hex(8)
    workspace = base / thread_id
    (workspace / ".boss").mkdir(parents=True)
    state: dict[str, Any] = {
        "thread_id": thread_id,
        "name": label,
        "turns": [],
        "plan": None,
        "approval": None,
        "write_key": None,
        "refusals": [],
    }
    _save(workspace, state)
    return _public(workspace, state)


def read_thread(root: Path, thread_id: str) -> dict[str, Any]:
    workspace = _workspace(root, thread_id)
    return _public(workspace, _load(workspace))


def post_turn(root: Path, thread_id: str, *, mode: str, text: str = "") -> dict[str, Any]:
    workspace = _workspace(root, thread_id)
    state = _load(workspace)
    kind = (mode or "").strip().lower()
    if kind not in _MODES:
        _refuse(workspace, state, "unknown_mode", "Use chat, plan, or build.")
    message = (text or "").strip()
    if kind in ("chat", "plan") and not message:
        _refuse(workspace, state, "text_required", "Write something first.")
    if kind == "chat":
        state["turns"].append({
            "mode": "chat",
            "text": message,
            "reply": "Chat only. No code was written.",
            "wrote_code": False,
        })
        _save(workspace, state)
        body = _public(workspace, state, mode="chat")
        body["reply"] = "Chat only. No code was written."
        return body
    if kind == "plan":
        # A new plan closes the gate. It never reaches the code writer below.
        state["plan"] = _make_plan(message)
        state["approval"] = None
        state["write_key"] = None
        state["turns"].append({
            "mode": "plan",
            "text": message,
            "reply": "Plan only. No code was written.",
            "wrote_code": False,
        })
        _save(workspace, state)
        body = _public(workspace, state, mode="plan")
        body["reply"] = "Plan only. No code was written."
        return body
    if not state.get("plan"):
        _refuse(workspace, state, "plan_required", "Plan the change before a build.")
    approval = state.get("approval") or {}
    if approval.get("plan_id") != state["plan"]["id"]:
        _refuse(
            workspace, state, "approval_required",
            "A person has to approve this plan before a build can write code.")
    key = f"build:{state['thread_id']}:{state['plan']['id']}:{approval['id']}"
    target = workspace / _CODE_FILE
    if state.get("write_key") == key and target.is_file():
        body = _public(workspace, state, mode="build")
        body["deduped"] = True
        body["wrote_code"] = False
        body["reply"] = "Already written. No second file."
        return body
    content = _render(state["plan"], approval)
    _write_code(workspace, content)  # the only write of a code file
    state["write_key"] = key
    state["turns"].append({
        "mode": "build",
        "text": message,
        "reply": "Wrote src/approved_build.py.",
        "wrote_code": True,
    })
    _save(workspace, state)
    body = _public(workspace, state, mode="build")
    body["wrote_code"] = True
    body["deduped"] = False
    body["reply"] = "Wrote src/approved_build.py."
    return body


def approve_plan(root: Path, thread_id: str, *, person_id: str) -> dict[str, Any]:
    workspace = _workspace(root, thread_id)
    state = _load(workspace)
    person = (person_id or "").strip()
    if person.lower() in _NOT_A_PERSON or not _PERSON.fullmatch(person):
        _refuse(workspace, state, "person_required", "Only a person can approve a build.")
    if not state.get("plan"):
        _refuse(workspace, state, "plan_required", "Plan the change before a build.")
    state["approval"] = {
        "id": "apr_" + secrets.token_hex(6),
        "plan_id": state["plan"]["id"],
        "person_id": person,
    }
    state["turns"].append({
        "mode": "approval",
        "text": person,
        "reply": "Approved. No code was written.",
        "wrote_code": False,
    })
    _save(workspace, state)
    body = _public(workspace, state, mode="approval")
    body["approved"] = True
    body["reply"] = "Approved. No code was written."
    return body


def _label(name: str) -> str:
    label = " ".join((name or "").split())
    if not label or any(mark in label for mark in ("/", "\\", "\x00")):
        raise BuilderRefused("bad_name", "Name a thread without a path.", status=400)
    if len(label) > 80:
        raise BuilderRefused("bad_name", "That thread name is too long.", status=400)
    return label


def _make_plan(text: str) -> dict[str, Any]:
    steps = [line.strip() for line in text.splitlines() if line.strip()]
    return {
        "id": "plan_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
        "steps": steps,
        "writes_code": False,
    }


def _render(plan: dict[str, Any], approval: dict[str, Any]) -> str:
    summary = json.dumps(" ".join(plan["steps"]))
    return "\n".join([
        '"""Code written only because a person approved the plan."""',
        "",
        f"PLAN_ID = {json.dumps(plan['id'])}",
        f"APPROVED_BY = {json.dumps(approval['person_id'])}",
        "",
        "def run() -> str:",
        f"    return {summary}",
        "",
    ])


def _write_code(workspace: Path, content: str) -> None:
    root = workspace.resolve()
    target = (root / _CODE_FILE).resolve()
    source = (root / "src").resolve()
    if target.parent != source or not str(target).startswith(str(root) + os.sep):
        raise BuilderRefused(
            "bad_path", "The build refused a path outside the thread.", status=409)
    source.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(target)


def _workspace(root: Path, thread_id: str) -> Path:
    if not _THREAD_ID.fullmatch(thread_id or ""):
        raise BuilderRefused(
            "thread_not_found", "That builder thread does not exist.", status=404)
    base = resolve_root(root)
    workspace = (base / thread_id).resolve()
    if workspace.parent != base or not (workspace / ".boss" / "thread.json").is_file():
        raise BuilderRefused(
            "thread_not_found", "That builder thread does not exist.", status=404)
    return workspace


def _load(workspace: Path) -> dict[str, Any]:
    raw = (workspace / ".boss" / "thread.json").read_text(encoding="utf-8")
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise BuilderRefused(
            "thread_unreadable", "The builder thread could not be read.", status=500)
    return loaded


def _save(workspace: Path, state: dict[str, Any]) -> None:
    ledger = workspace / ".boss" / "thread.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    temporary = ledger.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(ledger)


def _refuse(workspace: Path, state: dict[str, Any], code: str, message: str,
            status: int = 409) -> None:
    state.setdefault("refusals", []).append({"error": code, "message": message})
    _save(workspace, state)
    raise BuilderRefused(code, message, status=status)


def _code_files(workspace: Path) -> list[str]:
    found: list[str] = []
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace)
        if ".boss" in relative.parts:
            continue
        if path.suffix.lower() in CODE_SUFFIXES:
            found.append(relative.as_posix())
    return sorted(found)


def _public(workspace: Path, state: dict[str, Any], *, mode: str = "") -> dict[str, Any]:
    return {
        "thread_id": state["thread_id"],
        "name": state["name"],
        "mode": mode,
        "turns": list(state.get("turns") or []),
        "plan": state.get("plan"),
        "approval": state.get("approval"),
        "refusals": list(state.get("refusals") or []),
        "wrote_code": False,
        "deduped": False,
        "code_files": _code_files(workspace),
        "reply": "",
        "approved": bool(state.get("approval")),
    }
