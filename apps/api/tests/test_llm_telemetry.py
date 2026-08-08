"""VAG Phase 0: per-LLM-call telemetry (services/llm_telemetry.py) + the configurable
price table (openai_adapter). See docs/product/BOSS_VERIFICATION_AMPLIFIED_GENERATION.md."""
import threading

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from apps.api.app.config import get_settings
from apps.api.app.services import openai_adapter
from apps.api.app.services.llm_telemetry import (
    clear_llm_context,
    get_llm_context,
    record_llm_call,
    set_llm_context,
)
from apps.api.app.services.openai_adapter import (
    _with_retry,
    estimate_cost_usd_micros,
    price_for_model,
)


# --------------------------------------------------------------------------- #
#  Price table                                                                #
# --------------------------------------------------------------------------- #
def test_price_for_model_known_and_default(monkeypatch):
    monkeypatch.delenv("BOSS_MODEL_PRICES_JSON", raising=False)
    inp, out, known = price_for_model("gpt-4o-mini")
    assert (inp, out, known) == (0.15, 0.60, True)
    inp, out, known = price_for_model("some-unknown-model")
    assert (inp, out) == openai_adapter.DEFAULT_PRICE
    assert known is False


def test_price_for_model_matches_provider_prefixed_basename(monkeypatch):
    monkeypatch.delenv("BOSS_MODEL_PRICES_JSON", raising=False)
    # OpenRouter-style name: "deepseek/deepseek-chat" should match "deepseek-chat".
    inp, out, known = price_for_model("deepseek/deepseek-chat")
    assert known is True
    assert (inp, out) == openai_adapter.PRICE_PER_MTOK["deepseek-chat"]


def test_price_overrides_from_env_win(monkeypatch):
    # get_settings() is lru_cached — clear it around each env change.
    monkeypatch.setenv("BOSS_MODEL_PRICES_JSON", '{"gpt-4o": [1.0, 2.0], "my-local": [0.1, 0.2]}')
    get_settings.cache_clear()
    try:
        assert price_for_model("gpt-4o") == (1.0, 2.0, True)
        assert price_for_model("my-local") == (0.1, 0.2, True)
        # A malformed override must not break cost accounting — falls back to built-ins.
        monkeypatch.setenv("BOSS_MODEL_PRICES_JSON", "{not json")
        get_settings.cache_clear()
        assert price_for_model("gpt-4o") == (2.50, 10.00, True)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_estimate_cost_honors_self_reported_cost(monkeypatch):
    monkeypatch.delenv("BOSS_MODEL_PRICES_JSON", raising=False)
    assert estimate_cost_usd_micros({"cost_usd_micros": 1234, "model": "gpt-4o"}) == 1234
    # 1M prompt tokens at $0.27/Mtok + 1M completion at $1.10/Mtok, in micros.
    usage = {"model": "deepseek-chat", "prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert estimate_cost_usd_micros(usage) == int(round(1_000_000 * 0.27 + 1_000_000 * 1.10))


# --------------------------------------------------------------------------- #
#  Retry accounting                                                           #
# --------------------------------------------------------------------------- #
def test_with_retry_reports_retry_count(monkeypatch):
    class Transient(Exception):
        pass

    monkeypatch.setattr(openai_adapter, "_RETRYABLE", (Transient,))
    monkeypatch.setattr(openai_adapter.time, "sleep", lambda _s: None)
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Transient("429")
        return "ok"

    stats: dict = {}
    assert _with_retry(flaky, stats=stats) == "ok"
    assert stats["retries"] == 2


# --------------------------------------------------------------------------- #
#  Context + recording                                                        #
# --------------------------------------------------------------------------- #
_SQLITE_DDL = """
CREATE TABLE llm_calls (
    id TEXT PRIMARY KEY,
    project_id TEXT, code_task_run_id TEXT, stage TEXT, engine TEXT, model TEXT,
    role TEXT,
    prompt_tokens BIGINT NOT NULL DEFAULT 0,
    completion_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens BIGINT NOT NULL DEFAULT 0,
    cache_read_tokens BIGINT NOT NULL DEFAULT 0,
    cache_write_tokens BIGINT NOT NULL DEFAULT 0,
    cost_usd_micros BIGINT NOT NULL DEFAULT 0,
    price_estimated BOOLEAN NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    turns INTEGER NOT NULL DEFAULT 0,
    ok BOOLEAN NOT NULL DEFAULT 1,
    error TEXT
)
"""


def _telemetry_engine():
    # StaticPool: one shared in-memory DB across threads (sqlite:// is otherwise
    # per-connection, and the pool-thread test records from a worker thread).
    engine = create_engine(
        "sqlite://", future=True, poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as c:
        c.execute(text(_SQLITE_DDL))
    return engine


def test_record_llm_call_writes_row_with_context(monkeypatch):
    monkeypatch.delenv("BOSS_MODEL_PRICES_JSON", raising=False)
    engine = _telemetry_engine()
    set_llm_context(db_engine=engine, project_id="proj_1", run_id="ctr_1", stage="implement")
    record_llm_call(
        engine_name="native",
        usage={"model": "unknown-model", "prompt_tokens": 10, "completion_tokens": 5,
               "total_tokens": 15, "retries": 1},
        cost_usd_micros=42, latency_ms=1200, turns=1,
    )
    with engine.begin() as c:
        row = c.execute(text("SELECT * FROM llm_calls")).mappings().one()
    assert row["project_id"] == "proj_1"
    assert row["code_task_run_id"] == "ctr_1"
    assert row["stage"] == "implement"
    assert row["engine"] == "native"
    assert row["model"] == "unknown-model"
    assert row["prompt_tokens"] == 10
    assert row["cost_usd_micros"] == 42
    assert row["retries"] == 1
    assert bool(row["price_estimated"]) is True  # not in the price table -> estimate
    assert bool(row["ok"]) is True


def test_record_llm_call_explicit_stage_and_exact_cost(monkeypatch):
    engine = _telemetry_engine()
    set_llm_context(db_engine=engine, project_id="proj_1", run_id="ctr_2", stage="implement")
    # Self-reported cost (e.g. Claude CLI) => not an estimate, explicit stage wins.
    record_llm_call(
        engine_name="claude_code",
        usage={"model": "claude-opus", "cost_usd_micros": 999},
        cost_usd_micros=999, stage="review_code",
    )
    with engine.begin() as c:
        row = c.execute(text("SELECT * FROM llm_calls")).mappings().one()
    assert row["stage"] == "review_code"
    assert bool(row["price_estimated"]) is False


def test_record_llm_call_never_raises(monkeypatch):
    class ExplodingEngine:
        def begin(self):
            raise RuntimeError("db down")

    set_llm_context(db_engine=ExplodingEngine(), project_id="p", run_id="r", stage="s")
    # Must swallow the failure — telemetry never breaks the pipeline.
    record_llm_call(engine_name="native", usage={"model": "m"}, cost_usd_micros=1)


def test_context_does_not_leak_into_new_threads():
    set_llm_context(project_id="main_thread_project", run_id="main_thread_run")
    seen: dict = {}

    def child():
        seen.update(get_llm_context())

    t = threading.Thread(target=child)
    t.start()
    t.join()
    # A new thread starts with an empty context — pipeline panel closures must
    # (and do) call set_llm_context themselves inside the thread.
    assert "run_id" not in seen


def test_failed_call_is_recorded_with_error():
    engine = _telemetry_engine()
    set_llm_context(db_engine=engine, project_id="p", run_id="r", stage="implement")
    record_llm_call(engine_name="native", usage={"model": "m"}, ok=False,
                    error="boom " + "x" * 600)
    with engine.begin() as c:
        row = c.execute(text("SELECT * FROM llm_calls")).mappings().one()
    assert bool(row["ok"]) is False
    assert len(row["error"]) <= 500


def test_set_merge_none_clears_key_and_clear_empties():
    clear_llm_context()
    set_llm_context(project_id="p", run_id="r", stage="implement")
    set_llm_context(stage=None)  # None removes just that key
    ctx = get_llm_context()
    assert ctx.get("run_id") == "r" and "stage" not in ctx
    clear_llm_context()
    assert get_llm_context() == {}


def test_pool_thread_reuse_gets_each_closures_own_stage():
    """ThreadPoolExecutor REUSES threads: a closure that doesn't set its own full
    context would inherit the PREVIOUS closure's stage. Regression for the governed
    review panel — every LLM-calling closure sets its full context first."""
    from concurrent.futures import ThreadPoolExecutor

    engine = _telemetry_engine()

    def make_check(stage):
        def check():
            set_llm_context(db_engine=engine, project_id="p", run_id="r", stage=stage)
            record_llm_call(engine_name="native", usage={"model": "m"}, cost_usd_micros=1)
        return check

    with ThreadPoolExecutor(max_workers=1) as pool:  # forces thread reuse
        list(pool.map(lambda fn: fn(), [make_check("review_code"), make_check("security_check")]))
    with engine.begin() as c:
        stages = sorted(c.execute(text("SELECT stage FROM llm_calls")).scalars().all())
    assert stages == ["review_code", "security_check"]
