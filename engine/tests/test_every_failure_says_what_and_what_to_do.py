"""EVERY FAILURE SAYS WHAT HAPPENED AND WHAT TO DO (BOSSAPP-1428).

An enterprise install kept hitting failures whose words named a symptom, not the cause or a next
step: "BOSS couldn't save this to its database. Try again." for a full disk; "Connect a model in
Setup" about a model that was connected and errored; "The cause is not established: inspect the
test-author tool output"; "GitHub did not list the App's repositories. Try again." for an App
that was not installed. One contract now checks every person-facing failure sentence
(failure_words.problems): what happened in plain words, the next action and WHERE, no internal
field names, no secrets. This file runs it over every producer it can reach without a database,
and lints the source so a new vague sentence fails here before anyone reads it.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from boss_engine.services import decision_actions, failure_owner as fo, failure_words as fw
from boss_engine.services import model_failures as mf
from boss_engine.services import step_recovery as sr
from boss_engine.services.governed_code_task_service import write_tests_exhausted_message

ROOT = Path(__file__).resolve().parents[2]
SVC = ROOT / "engine" / "boss_engine" / "services"

FAKE_TOKEN = "sk-ant-api03-" + "x" * 40


def _ok(what: str, action: str, where: object = None) -> None:
    found = fw.problems(what=what, action=action, where=str(where) if where else None)
    assert not found, f"{found}\n  what:   {what}\n  action: {action}"


# ── the contract itself ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("what, action, where, expect", [
    ("Something went wrong.", "Open Setup › Models.", None, "vague"),
    ("Failed.", "Open Setup › Models.", None, "bare status"),
    ("", "Open Setup › Models.", None, "what: empty"),
    ("GitHub did not list the repositories.", "Try again.", None, "bare 'Try again.'"),
    ("GitHub did not list the repositories.", "Retry.", None, "bare 'Retry.'"),
    ("The build stopped.", "Please look into it.", None, "no place"),
    ("The cause is not established.", "Open Activity.", None, "vague"),
    ("No tests.", "Inspect the test-author tool output, then open Activity.", None, "vague"),
    ("A preview repair must name extends_run_id.", "Pick a build in Workflows.", None,
     "internal name 'extends_run_id'"),
    ("The token " + FAKE_TOKEN + " was refused.", "Reconnect it in Setup.", None, "secret"),
])
def test_the_contract_refuses_each_kind_of_unusable_sentence(what, action, where, expect):
    found = fw.problems(what=what, action=action, where=where)
    assert any(expect in p for p in found), found


@pytest.mark.parametrize("what, action, where", [
    ("BOSS's database is out of disk space.", "Nothing to do — it resumes by itself.", None),
    ("The login was rejected.", "Reconnect it in Setup › Models.", None),
    ("The host is out of disk.", "Run `boss doctor` on the host.", None),
    ("Held by the budget.", "Raise the cap.", "/?area=budget"),
    ("The step failed: `KeyError: 'x_y'`.", "Press Retry on this step.", None),
])
def test_the_contract_accepts_an_exact_sentence(what, action, where):
    assert fw.problems(what=what, action=action, where=where) == []


def test_there_is_one_vague_list():
    from boss_engine.services import failure_guidance

    assert failure_guidance.GENERIC is fw.VAGUE_PHRASES


# ── model_failures.explain: every kind it can say ────────────────────────────────────────────

def _db_error(cls_name: str, message: str):
    from sqlalchemy.exc import IntegrityError, OperationalError

    orig_cls = type(cls_name, (Exception,), {})
    orig_cls.__module__ = "psycopg.errors"
    wrapper = IntegrityError if "Violation" in cls_name else OperationalError
    return wrapper("INSERT INTO events (id) VALUES (%(id)s)", {"id": "evt_secret_value"},
                   orig_cls(message))


def _corpus() -> list[tuple[str, object, str | None]]:
    """(label, error, source_id) — one sample per kind explain() can return, and per storage
    cause. A new kind in explain() fails test_every_kind_has_a_sample until one is added."""
    from boss_engine.services import harness_failures
    from boss_engine.services.model_router.failover import RouteRefused
    from boss_engine.services.model_routing import (
        PROVIDER_REQUIREMENTS,
        ProviderRequirementMissing,
    )

    denial = ("bwrap: No permissions to create a new namespace, likely because the kernel does "
              "not allow non-privileged user namespaces.")
    verdict = harness_failures.classify(denial, engine="codex", where="tool_result", ok=False)
    req = next(iter(PROVIDER_REQUIREMENTS.values()))
    return [
        ("storage: disk full", _db_error("DiskFull", "could not extend file: No space left on "
                                                     "device"), None),
        ("storage: connection", _db_error("AdminShutdown", "terminating connection due to "
                                                           "administrator command"), None),
        ("storage: duplicate", _db_error("UniqueViolation", 'duplicate key value violates '
                                                            'unique constraint "idx_x"'), None),
        ("storage: busy", _db_error("DeadlockDetected", "deadlock detected"), None),
        ("storage: foreign key", _db_error("ForeignKeyViolation", 'insert or update on table '
                                           '"x" violates foreign key constraint "x_fk"'), None),
        ("storage: not null", _db_error("NotNullViolation", 'null value in column "a" violates '
                                                            'not-null constraint'), None),
        ("storage: other", _db_error("DataError", "invalid input syntax for type uuid"), None),
        ("storage: text", "current transaction is aborted, commands ignored until end", None),
        ("route_refused", RouteRefused("Local means local: no local model is connected for "
                                       "this work."), None),
        ("credential_misdirected", "The OpenAI key is sent only to api.openai.com; refusing to "
                                   "send it to proxy.example.com.", None),
        ("harness_failure", verdict.sentence(), "codex"),
        ("credential_store_wait", "BOSS could not reach its secret store to read the login",
         None),
        ("credential_unreadable", "BOSS could not read the stored Claude login", "claude_code"),
        ("capacity_wait", "Selected model source has no concurrent lease capacity",
         "claude_code"),
        ("login_rejected", "Invalid bearer token", "claude_code"),
        ("budget_hold", mf.budget_hold_sentence(
            "red", 0.95, 10_000_000,
            {"whose": "today's", "resets_at_label": "00:00 UTC", "cap_name": "daily cap"}),
         None),
        ("budget_hold: reconcile", "budget is held for an unresolved provider outcome", None),
        ("budget_wait: admission", "Budget admission is busy with another transaction.", None),
        ("budget_wait: headroom", "Budget headroom is held by other work's active "
                                  "reservations.", None),
        ("budget_wait: burst", "Burst cap exceeded ($/min). Retry shortly.", None),
        ("budget_refused: period", "budget cap exceeded: today's budget is used ($10.20 of $10)"
                                   ". It resets at 00:00 UTC, or raise the daily cap in Govern "
                                   "› Limits.", None),
        ("budget_refused", "Budget reservation denied", None),
        ("capacity_owner", "this subscription is owner scoped and cannot be pooled",
         "claude_code"),
        ("provider_requirement", ProviderRequirementMissing("image_skill", req), None),
        ("no_model", "no_eligible_source", None),
        ("no_direct_answer", f"the model {mf.NO_DIRECT_ANSWER}", None),
        ("refused_by_api", "Client error '409 Conflict' for the worker lease", None),
        ("reconcile", "reconciliation required before retry", None),
        ("failed", RuntimeError("upstream answered 500 with an empty body"), None),
        ("failed: secret", RuntimeError(f"call refused for key {FAKE_TOKEN}"), None),
    ]


def _explain_kinds() -> set[str]:
    tree = ast.parse((SVC / "model_failures.py").read_text(encoding="utf-8"))
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)
           and n.name in ("explain", "_explain")]
    kinds = set()
    for node in (node for fn in fns for node in ast.walk(fn)):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "kind"
                        and isinstance(value, ast.Constant)):
                    kinds.add(value.value)
    return kinds


def test_every_kind_has_a_sample():
    kinds = _explain_kinds()
    assert len(kinds) >= 15, "the scan no longer sees explain()'s kinds"
    produced = {mf.explain(error, source_id=source)["kind"] for _l, error, source in _corpus()}
    missing = kinds - produced
    assert not missing, (f"explain() can return {sorted(missing)} but the corpus has no sample "
                         "for it: add one to _corpus()")


@pytest.mark.parametrize("label, error, source", _corpus(), ids=[c[0] for c in _corpus()])
def test_every_explained_failure_says_what_and_what_to_do(label, error, source):
    said = mf.explain(error, source_id=source)
    _ok(said["headline"], said["action"], said.get("href"))
    turn = mf.explain_turn_failure(error, source_id=source)
    _ok(turn["headline"], turn["action"], turn.get("href"))
    assert FAKE_TOKEN not in str(said) and FAKE_TOKEN not in str(turn)


def test_each_database_cause_has_its_own_sentence():
    said = {label: mf.explain(error) for label, error, _s in _corpus()
            if label.startswith("storage")}
    assert {s["kind"] for s in said.values()} == {"storage"}
    causes = {label: s["storage_cause"] for label, s in said.items()}
    assert causes["storage: disk full"] == "disk_full"
    assert causes["storage: connection"] == "connection_lost"
    assert causes["storage: duplicate"] == "duplicate"
    assert causes["storage: busy"] == causes["storage: text"] == "busy"
    assert causes["storage: foreign key"] == causes["storage: not null"] == "constraint"
    assert causes["storage: other"] == "other"
    disk = said["storage: disk full"]
    assert "out of disk space" in disk["headline"] and "`boss doctor`" in disk["action"]
    assert "Govern › Infrastructure" in disk["action"]
    assert said["storage: duplicate"]["action"].startswith("Reload")
    assert "already saved" in said["storage: duplicate"]["headline"]
    assert "reconnects by itself" in said["storage: connection"]["action"]
    assert "a value it needs was missing" in said["storage: not null"]["headline"]
    other = said["storage: other"]
    assert "invalid input syntax for type uuid" in other["headline"]
    # Never the statement, its values, or the driver's class path.
    for s in said.values():
        text = s["headline"] + s["action"]
        assert "INSERT INTO" not in text and "evt_secret_value" not in text
        assert "psycopg" not in text


def test_a_turn_that_failed_says_its_one_line_cause():
    said = mf.explain_turn_failure(RuntimeError("boom at step 3"))
    assert said["sentence"] == ("BOSS couldn't finish this turn: `RuntimeError: boom at step 3`."
                                " Try again; if it repeats, Details shows the error.")


def test_a_planted_secret_is_never_echoed():
    error = RuntimeError(f"Authorization failed for {FAKE_TOKEN} at the proxy")
    for said in (mf.explain(error), mf.explain_turn_failure(error)):
        assert FAKE_TOKEN not in said["headline"] + said["action"]
    message = write_tests_exhausted_message(stronger_lane=False,
                                            last_words=f"I used {FAKE_TOKEN} to log in")
    assert FAKE_TOKEN not in message


# ── failure_owner: every person-side rule and classify() result ──────────────────────────────

_PERSON_SAMPLES = {
    "budget": "error_max_budget_usd: the run hit its spending cap",
    "no_repository": "No GitHub credentials for acme/shop",
    "repo_access": "remote: Repository not found.",
    "toolchain": "Go is not available: run boss toolchains enable go",
    "registry_access": "npm ERR! code E401 Unable to authenticate, need: Basic realm",
    "connector_login": "mcp server answered 401",
    "model_login": "Not logged in · Please run /login",
    "grant": "no person's grant covers this work",
    "approval_expired": "the approval expired before anybody answered",
    "merge_conflict": "the branch does not merge cleanly with its base",
    "host": "Host key verification failed.",
}


def test_every_person_rule_has_a_sample():
    assert set(_PERSON_SAMPLES) == {code for code, _p, _a in fo._PERSON_RULES}


@pytest.mark.parametrize("code", sorted(_PERSON_SAMPLES))
def test_every_person_rule_says_what_and_what_to_do(code):
    sample = _PERSON_SAMPLES[code]
    cls, cause, action = fo.classify(sample)
    assert (cls, cause) == (fo.PERSON, code), (cls, cause)
    _ok(fo._WHY_PERSON[code], action)


@pytest.mark.parametrize("error, kw, what", [
    ("engine_write_unavailable: bwrap denied", {},
     "The coding engine cannot write files on this host."),
    ("the work needs an input only you have", {"failure_class": "needs_human"},
     fo._WHY_PERSON["needs_human"]),
])
def test_every_classified_person_action_says_where(error, kw, what):
    cls, _cause, action = fo.classify(error, **kw)
    assert cls == fo.PERSON
    _ok(what, action)


# ── step_recovery: every ask a person reads ──────────────────────────────────────────────────

_FAILURE = {"headline": "The deploy of the app failed its health check"}
_TRIAGE = {"action": "retry", "cause": "the health check timed out"}


@pytest.mark.parametrize("kw", [
    {"refused": "not the JSON object asked for"},
    {"error": "RuntimeError: no model connected"},
    {"error": ""},
    {"error": "TimeoutError: the provider did not answer within 120s"},
    {"error": "RuntimeError: Your credit balance is too low to access the Anthropic API.",
     "provider": "anthropic"},
], ids=["refused", "no model connected", "nothing said", "model call errored", "out of credit"])
def test_no_model_ask_says_what_and_what_to_do(kw):
    asked = sr.no_model_ask(_FAILURE, _TRIAGE, **kw)
    _ok(asked["question"], asked["question"])
    _ok(asked["question"], asked["after"])


def test_a_connected_model_that_errored_is_never_told_to_connect_a_model():
    """The enterprise RCA's case: the recovery model WAS connected; its call errored."""
    asked = sr.no_model_ask(_FAILURE, _TRIAGE,
                            error="TimeoutError: the provider did not answer within 120s")
    assert "Connect a model" not in asked["question"]
    assert "did not answer (TimeoutError: the provider did not answer within 120s)" in \
        asked["question"]
    assert "changed nothing" in asked["question"] and "Setup › Models" in asked["question"]
    none = sr.no_model_ask(_FAILURE, _TRIAGE, error="RuntimeError: no model connected")
    assert "Connect a model in Setup › Models" in none["question"]


def test_the_other_asks_say_what_and_what_to_do():
    unsure = sr.unsure_ask(_FAILURE, {"action": "retry", "confidence": 0.3,
                                      "cause": "the health check timed out"}, agent="Harbor")
    _ok(unsure["question"], unsure["after"])
    waiver = sr.waiver_ask({"failing_gates": ["security review"], "build_run_id": "crun_1"})
    _ok(waiver["question"], waiver["question"])
    _ok(waiver["question"], waiver["after"])
    for repeated in (False, True):
        asked = sr._ask({**_FAILURE, "action": "Free port 8000 on the host"}, {},
                        repeated=repeated)
        _ok(asked["question"], asked["after"])


# ── decision_actions: every refusal card ─────────────────────────────────────────────────────

@pytest.mark.parametrize("family", sorted(decision_actions._REFUSAL_GUIDANCE))
def test_every_refusal_card_says_what_and_what_to_do(family):
    card = decision_actions._REFUSAL_GUIDANCE[family]
    _ok(card["what_happened"], card["what_to_do"], card.get("area"))


# ── the test author that wrote nothing ───────────────────────────────────────────────────────

@pytest.mark.parametrize("stronger", [False, True])
@pytest.mark.parametrize("last_words, changed", [
    ("", []),
    ("I could not find where this repo keeps its tests.", ["src/app.py", "README.md"]),
    ("x" * 400, [f"f{i}.py" for i in range(9)]),
])
def test_the_test_author_message_states_what_was_observed(stronger, last_words, changed):
    message = write_tests_exhausted_message(stronger_lane=stronger, last_words=last_words,
                                            changed=changed)
    _ok(message, message)
    assert "inspect" not in message.lower() and "not established" not in message
    if changed:
        assert changed[0] in message and "none of them tests" in message
    else:
        assert "It changed no files." in message
    assert len(message) < 700


# ── shared wordings ──────────────────────────────────────────────────────────────────────────

class _Refused(Exception):
    def __init__(self, status):
        super().__init__("repositories_unavailable")
        self.extra = {"status": status}


@pytest.mark.parametrize("error, says", [
    (_Refused(401), "Reconnect the GitHub account in Setup › Repositories"),
    (_Refused(403), "Install the App on it"),
    (_Refused(404), "HTTP 404"),
    (_Refused(502), "GitHub is unavailable"),
    (TimeoutError("read timed out"), "GitHub is unavailable"),
    (RuntimeError("weird"), "Press Test on the account"),
])
def test_a_github_listing_failure_says_githubs_answer(error, says):
    sentence = fw.github_refusal("the App's repositories", error)
    assert says in sentence
    lead, _, rest = sentence.partition(": ")
    _ok(sentence, rest)


def test_the_agent_that_replied_without_a_plan_is_quoted():
    from boss_engine.services import agent_converse

    with pytest.raises(ValueError) as caught:
        agent_converse._only_json("I think we should first talk about the database schema "
                                  "and the migration order before anything else is built")
    said = str(caught.value)
    assert said.startswith("The agent replied without a plan (it said: 'I think we should")
    assert said.endswith("Try again, or rephrase what you want built.")
    assert len(said) < 200


def test_a_rejected_credential_names_who_rejected_it():
    from boss_engine.services.agent_health import plain_cause

    assert plain_cause("401 Unauthorized for url https://hooks.example.org/v1/x") == \
        "hooks.example.org rejected BOSS's credential (HTTP 401)"
    assert plain_cause("Grafana answered 401") == "the Grafana token was rejected"


def test_the_self_fix_time_cap_tells_queue_wait_from_run_time():
    from boss_engine.services.self_fix_build import time_cap_words

    ran = time_cap_words({"created_at": "2026-11-20T10:00:00+00:00",
                          "started_at": "2026-11-20T14:00:00+00:00"}, 71, 60)
    assert ran.startswith("it ran 71 min after waiting 240 min in the queue, past its 60 min")
    queued = time_cap_words({"created_at": "2026-11-20T10:00:00+00:00"}, 311, 60)
    assert queued.startswith("311 min have passed since it was queued")
    for words in (ran, queued):
        assert "Self-fix › Time in Govern" in words and "Agent capacity" in words


# ── the lint: no vague failure sentence in the source ────────────────────────────────────────

#: The modules people read failures from, and every service and API module for the strong
#: phrases. A new file is covered by the glob.
_SCANNED = sorted({*SVC.rglob("*.py"), *(ROOT / "apps" / "api" / "app").rglob("*.py"),
                   ROOT / "engine" / "boss_engine" / "identity" / "github_accounts.py",
                   ROOT / "engine" / "boss_engine" / "multirepo" / "registry.py",
                   ROOT / "engine" / "boss_engine" / "oidc_provider" / "hosted.py"})

#: The contract's own definition of the phrases, which must name them.
_ALLOWED = {SVC / "failure_words.py"}

_ENDS_IN_RETRY = re.compile(r"(?:^|(?<=[.!?])\s+)(?:Please )?(?:try again|retry)"
                            r"(?: later| shortly| in a moment)?\.?\s*$", re.I)
_SYMPTOM = re.compile(r"\b(?:did not|didn't|couldn't|could not|cannot|can't|failed|unable|"
                      r"not work|went wrong|error)\b", re.I)
_CAUSE = re.compile(r"[:(]|\bbecause\b| — |\bso\b", re.I)


def _literals(source: str):
    """(line, text) for every string constant and f-string (its literal parts joined) — not
    docstrings, which may quote the old sentence they replaced."""
    tree = ast.parse(source)
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)}
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            yield node.lineno, "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{…}"
                for v in node.values)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docs:
            yield node.lineno, node.value


def lint(source: str) -> list[str]:
    """Each vague failure sentence in a module's source: a vague phrase anywhere, or a
    sentence that ends "Try again." after naming only a symptom — no cause, no place."""
    hits: dict[tuple[int, str], str] = {}     # one hit per line and rule (an f-string's parts)
    for line, text in _literals(source):
        for phrase in fw.vague_in(text):
            hits.setdefault((line, phrase.lower()), f"{line}: vague {phrase!r} in {text[:100]!r}")
        stripped = text.strip()
        end = _ENDS_IN_RETRY.search(stripped)
        rest = stripped[:end.start()].strip() if end else ""
        if rest and _SYMPTOM.search(rest) and not _CAUSE.search(rest):
            hits.setdefault((line, "retry"),
                            f"{line}: symptom then 'try again', no cause: {text[:100]!r}")
    return list(hits.values())


def test_the_lint_catches_a_planted_vague_message():
    planted = '''
def f(error):
    raise ValueError("Something went wrong.")

def g():
    return "GitHub did not list the App's repositories. Try again."

def h(name):
    return f"{name}'s login could not start inside BOSS. Try again."

def i():
    return "The cause is not established: inspect the test-author tool output first."

def ok():
    return ("GitHub did not list the App's repositories: it refused BOSS's sign-in "
            "(HTTP 401). Reconnect the account in Setup › Repositories.")
'''
    hits = lint(planted)
    assert len(hits) == 5, hits          # i() carries two phrases
    assert not [h for h in hits if "Reconnect" in h]


def test_no_module_says_a_vague_failure():
    assert len(_SCANNED) > 300, "the scan no longer sees the service and API modules"
    found = []
    for path in _SCANNED:
        if path in _ALLOWED:
            continue
        for hit in lint(path.read_text(encoding="utf-8")):
            found.append(f"{path.relative_to(ROOT)}:{hit}")
    assert not found, ("A failure sentence names a symptom only — say what happened and what "
                       "to do (failure_words.problems):\n" + "\n".join(found))


def test_explain_keeps_the_chat_wording_out_of_its_payload():
    """Callers spread explain() into API payloads; the turn wording is explain_turn_failure's."""
    from boss_engine.services.model_failures import explain

    said = explain("duplicate key value violates unique constraint \"x_key\"")
    assert said["kind"] == "storage" and "turn_action" not in said


def test_an_app_level_github_refusal_names_the_app_not_an_account():
    from boss_engine.services.failure_words import github_refusal, problems

    said = github_refusal("the BOSS app's installations", status=401, app_level=True)
    assert "GitHub App itself" in said and "Reconnect the GitHub account" not in said
    assert problems(what=said, action=said) == []
