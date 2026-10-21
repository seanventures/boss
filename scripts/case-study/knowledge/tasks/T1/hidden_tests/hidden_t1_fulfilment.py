"""T1 hidden acceptance suite: northpeak-fulfilment reserves stock on Saleor's order webhook.

Never shown to agents. run.py starts the service BOSS built against a scratch Postgres and runs
this suite with:
  NORTHPEAK_URL               e.g. http://127.0.0.1:8080
  NORTHPEAK_DB_DSN            the service's DATABASE_URL
  NORTHPEAK_WEBHOOK_SECRET    the secret the service was started with
The fixture creates the slice of Saleor's warehouse_stock table that the contract reads.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

URL = os.environ.get("NORTHPEAK_URL", "")
DSN = os.environ.get("NORTHPEAK_DB_DSN", "")
SECRET = os.environ.get("NORTHPEAK_WEBHOOK_SECRET", "")
pytestmark = pytest.mark.skipif(not (URL and DSN and SECRET), reason="run by the case-study harness")


def post(body: dict, *, secret: str = None, raw: bytes = None):
    data = raw if raw is not None else json.dumps(body).encode()
    sig = hmac.new((secret or SECRET).encode(), data, hashlib.sha256).hexdigest()
    req = urllib.request.Request(f"{URL}/webhooks/saleor/order-created", data=data, method="POST",
                                 headers={"Content-Type": "application/json", "Saleor-Signature": sig})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, {}


@pytest.fixture
def db():
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS warehouse_stock (
            id SERIAL PRIMARY KEY, product_variant_id TEXT NOT NULL, warehouse_id TEXT NOT NULL,
            quantity INT NOT NULL, quantity_allocated INT NOT NULL DEFAULT 0)""")
        yield conn


def variant(db, *stocks: tuple[int, int]) -> str:
    vid = "var-" + uuid.uuid4().hex[:10]
    for i, (qty, allocated) in enumerate(stocks):
        db.execute("INSERT INTO warehouse_stock (product_variant_id, warehouse_id, quantity, quantity_allocated) "
                   "VALUES (%s, %s, %s, %s)", (vid, f"wh-{i}", qty, allocated))
    return vid


def order(*lines: tuple[str, int]) -> tuple[dict, list[str]]:
    ids = ["line-" + uuid.uuid4().hex[:10] for _ in lines]
    return ({"order": {"id": "order-" + uuid.uuid4().hex[:8],
                       "lines": [{"id": i, "variant_id": v, "quantity": q} for i, (v, q) in zip(ids, lines)]}}, ids)


def reserved(db, line_id: str):
    return db.execute("SELECT quantity, expires_at - created_at FROM stock_reservation WHERE order_line_id = %s",
                      (line_id,)).fetchone()


def test_health():
    with urllib.request.urlopen(f"{URL}/healthz", timeout=5) as resp:
        assert resp.status == 200


def test_a_bad_signature_is_refused_and_reserves_nothing(db):
    v = variant(db, (5, 0))
    body, (line,) = order((v, 1))
    status, _ = post(body, secret="not-the-secret")
    assert status == 401
    assert reserved(db, line) is None


def test_an_order_reserves_each_line_for_fifteen_minutes(db):
    a, b = variant(db, (5, 0)), variant(db, (3, 1), (2, 0))
    body, (la, lb) = order((a, 2), (b, 4))
    status, out = post(body)
    assert status == 200 and sorted(out["reserved"]) == sorted([la, lb]) and out["rejected"] == []
    qty, ttl = reserved(db, la)
    assert qty == 2 and abs(ttl - timedelta(minutes=15)) < timedelta(seconds=5)
    assert reserved(db, lb)[0] == 4          # 3-1 + 2-0 across two warehouses


def test_a_repeated_webhook_never_reserves_twice(db):
    v = variant(db, (3, 0))
    body, (line,) = order((v, 2))
    assert post(body)[0] == 200
    status, _ = post(body)
    assert status == 200
    rows = db.execute("SELECT count(*), sum(quantity) FROM stock_reservation WHERE variant_id = %s", (v,)).fetchone()
    assert rows == (1, 2)


def test_a_line_that_cannot_be_fully_reserved_reserves_nothing(db):
    plenty, scarce = variant(db, (10, 0)), variant(db, (4, 2))
    body, (lp, ls) = order((plenty, 1), (scarce, 3))
    status, out = post(body)
    assert status == 200 and out["reserved"] == [lp] and out["rejected"] == [ls]
    assert reserved(db, ls) is None


def test_active_reservations_count_and_expired_ones_do_not(db):
    v = variant(db, (3, 0))
    now = datetime.now(timezone.utc)
    db.execute("INSERT INTO stock_reservation VALUES (%s, %s, 2, %s, %s)", ("old-" + v, v, now - timedelta(hours=1),
                                                                            now - timedelta(minutes=45)))
    db.execute("INSERT INTO stock_reservation VALUES (%s, %s, 2, %s, %s)", ("live-" + v, v, now, now + timedelta(minutes=15)))
    body, (line,) = order((v, 2))
    assert post(body)[1]["rejected"] == [line]          # 3 - 2 live = 1 left
    body, (line,) = order((v, 1))
    assert post(body)[1]["reserved"] == [line]


def test_two_orders_racing_for_the_last_unit_never_oversell(db):
    v = variant(db, (1, 0))
    results = []
    bodies = [order((v, 1))[0] for _ in range(8)]
    threads = [threading.Thread(target=lambda b=b: results.append(post(b))) for b in bodies]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(status == 200 for status, _ in results)
    total = db.execute("SELECT coalesce(sum(quantity), 0) FROM stock_reservation WHERE variant_id = %s", (v,)).fetchone()[0]
    assert total == 1
