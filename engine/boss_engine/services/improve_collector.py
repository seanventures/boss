"""The collector: where installs' heartbeats and "Improve BOSS" batches land (collector mode).

docs/product/RUN_SCORECARDS.md ("Collector"). Part of BOSS itself for now, running on goldeneye,
active only when an operator turns Collector mode on in Govern › Sharing; every route answers
404 otherwise. Unauthenticated by design (installs have no account), so everything that arrives
is bounded and validated before it is stored:

  * ≤ 256 KB compressed, ≤ 2 MB after a BOUNDED gunzip (a zip bomb stops at the cap);
  * schema version 1, validated by the same allow-list the client uses (unknown fields dropped);
  * 30 requests per IP per hour and 6 per install id per day, in memory. The IP is a bucket key
    for one hour and is never written anywhere;
  * idempotent per (install_id, day) for the heartbeat and per (install_id, batch day) for each
    batch: a repeat answers {"ok": true, "duplicate": true}.

Stored by the random install id only, kept 18 months, then rolled into monthly counts and deleted.
"""
from __future__ import annotations

import collections
import csv
import hashlib
import hmac
import io
import json
import threading
import time
import zlib
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

from . import improve_allowlist as allow

MAX_GZ = 256 * 1024
MAX_RAW = 2 * 1024 * 1024
IP_PER_HOUR = 30
INSTALL_PER_DAY = 6
RETENTION_DAYS = 548  # 18 months
DELETE_SKEW_S = 600


class CollectorError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def enabled(connection) -> bool:
    from .improve_boss import switches

    return bool(switches(connection)["collector"]["on"])


# ------------------------------------------------------------------------------------------
# Limits
# ------------------------------------------------------------------------------------------


class _Window:
    """A sliding-window counter per key, bounded in keys (oldest evicted)."""

    def __init__(self, limit: int, seconds: int, max_keys: int = 20_000):
        self.limit, self.seconds, self.max_keys = limit, seconds, max_keys
        self.hits: "collections.OrderedDict[str, collections.deque]" = collections.OrderedDict()
        self.lock = threading.Lock()

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        with self.lock:
            q = self.hits.get(key)
            if q is None:
                q = collections.deque()
                self.hits[key] = q
                while len(self.hits) > self.max_keys:
                    self.hits.popitem(last=False)
            self.hits.move_to_end(key)
            while q and now - q[0] > self.seconds:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self.lock:
            self.hits.clear()


IP_LIMIT = _Window(IP_PER_HOUR, 3600)
INSTALL_LIMIT = _Window(INSTALL_PER_DAY, 86400)
#: NEW install ids one address may introduce per day before the rest are marked suspicious and
#: kept out of every count (the proof card cannot be inflated by minting ids).
NEW_INSTALLS_PER_IP_DAY = 3
NEW_INSTALL_LIMIT = _Window(NEW_INSTALLS_PER_IP_DAY, 86400)


def ip_key(ip: str) -> str:
    """The bucket key for an address: a hash, held in memory for an hour, never stored."""
    return hashlib.sha256(("boss-collector|" + (ip or "")).encode()).hexdigest()[:24]


def unpack(body: bytes, content_encoding: Optional[str], *, cap_gz: int = MAX_GZ) -> Any:
    """Bounded: refuse big bodies, inflate at most MAX_RAW bytes, parse JSON."""
    if len(body) > cap_gz:
        raise CollectorError(413, "too_large")
    raw = body
    if (content_encoding or "").lower().strip() == "gzip" or body[:2] == b"\x1f\x8b":
        try:
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            raw = d.decompress(body, MAX_RAW + 1)
            if len(raw) > MAX_RAW or d.unconsumed_tail:
                raise CollectorError(413, "too_large_inflated")
        except zlib.error as error:
            raise CollectorError(400, "bad_gzip") from error
    elif len(raw) > MAX_RAW:
        raise CollectorError(413, "too_large")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise CollectorError(400, "bad_json") from error


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ------------------------------------------------------------------------------------------
# Ingest, delete, share
# ------------------------------------------------------------------------------------------


def ingest(connection, payload: Any, *, ip: str = "") -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("v") != allow.SCHEMA_VERSION:
        raise CollectorError(422, "unsupported_version")
    clean, dropped = allow.sanitize(payload, allow.ENVELOPE)
    install_id, day_s = clean.get("install_id"), clean.get("day")
    if not install_id or not day_s:
        raise CollectorError(422, "invalid_envelope")
    day = date.fromisoformat(day_s)
    today = _today()
    if not today - timedelta(days=8) <= day <= today + timedelta(days=1):
        raise CollectorError(422, "day_out_of_range")
    if not IP_LIMIT.allow(ip_key(ip)):
        raise CollectorError(429, "rate_limited_ip")
    if not INSTALL_LIMIT.allow(install_id):
        raise CollectorError(429, "rate_limited_install")
    hb = clean.get("heartbeat") or {}
    batches = clean.get("batches") or []
    known = connection.execute(text("SELECT 1 FROM tm_installs WHERE install_id = :id"),
                               {"id": install_id}).scalar()
    suspicious = bool(not known and not NEW_INSTALL_LIMIT.allow(ip_key(ip)))
    ctx = (batches[-1].get("context") if batches else None) or {}
    connection.execute(text("""
        INSERT INTO tm_installs (install_id, secret_hash, first_seen_day, last_seen_day, version,
                                 edition, install_method, os, arch, improve_on, suspicious)
        VALUES (:id, :h, :d, :d, :v, :e, :m, :os, :arch, :imp, :sus)
        ON CONFLICT (install_id) DO UPDATE SET
          secret_hash = COALESCE(tm_installs.secret_hash, EXCLUDED.secret_hash),
          first_seen_day = LEAST(tm_installs.first_seen_day, EXCLUDED.first_seen_day),
          last_seen_day = GREATEST(tm_installs.last_seen_day, EXCLUDED.last_seen_day),
          version = COALESCE(EXCLUDED.version, tm_installs.version),
          edition = COALESCE(EXCLUDED.edition, tm_installs.edition),
          install_method = COALESCE(EXCLUDED.install_method, tm_installs.install_method),
          os = COALESCE(EXCLUDED.os, tm_installs.os),
          arch = COALESCE(EXCLUDED.arch, tm_installs.arch),
          improve_on = EXCLUDED.improve_on"""),
        {"id": install_id, "h": clean.get("secret_hash"), "d": day,
         "v": hb.get("version") or ctx.get("version"), "e": hb.get("edition") or ctx.get("edition"),
         "m": hb.get("install_method"), "os": hb.get("os"), "arch": hb.get("arch"),
         "imp": bool(batches), "sus": suspicious})
    new = 0
    if hb:
        new += connection.execute(text("""
            INSERT INTO tm_heartbeats (install_id, day, version, edition, install_method, os, arch,
                                       age_bucket, worked_24h)
            VALUES (:id, :d, :v, :e, :m, :os, :arch, :age, :w) ON CONFLICT DO NOTHING"""),
            {"id": install_id, "d": day, "v": hb.get("version"), "e": hb.get("edition"),
             "m": hb.get("install_method"), "os": hb.get("os"), "arch": hb.get("arch"),
             "age": hb.get("age"), "w": bool(hb.get("worked_24h"))}).rowcount
    for b in batches:
        if not b.get("day"):
            continue
        new += connection.execute(text("""
            INSERT INTO tm_batches (install_id, day, body) VALUES (:id, :d, CAST(:b AS JSONB))
            ON CONFLICT DO NOTHING"""),
            {"id": install_id, "d": date.fromisoformat(b["day"]), "b": json.dumps(b)}).rowcount
    return {"ok": True, "duplicate": new == 0, "stored": new, "dropped_fields": dropped}


def delete(connection, payload: Any, *, now: Optional[float] = None) -> dict[str, Any]:
    """A signed delete: the secret whose hash we hold, and an HMAC over the id and a fresh time."""
    if not isinstance(payload, dict):
        raise CollectorError(400, "bad_request")
    install_id, proof = str(payload.get("install_id") or ""), str(payload.get("proof") or "")
    sig = str(payload.get("sig") or "")
    try:
        ts = int(payload.get("ts"))
    except (TypeError, ValueError) as error:
        raise CollectorError(400, "bad_request") from error
    if abs((now or time.time()) - ts) > DELETE_SKEW_S:
        raise CollectorError(401, "stale")
    stored = connection.execute(text("SELECT secret_hash FROM tm_installs WHERE install_id = :id"),
                                {"id": install_id}).scalar()
    if not stored:
        return {"ok": True, "deleted": 0}  # nothing held: the answer is the same either way
    want = hmac.new(proof.encode(), f"delete|{install_id}|{ts}".encode(),
                    hashlib.sha256).hexdigest()
    if not (hmac.compare_digest(hashlib.sha256(proof.encode()).hexdigest(), stored)
            and hmac.compare_digest(want, sig)):
        raise CollectorError(401, "bad_signature")
    n = 0
    for table in ("tm_heartbeats", "tm_batches", "tm_shares", "tm_installs"):
        n += connection.execute(text(f"DELETE FROM {table} WHERE install_id = :id"),
                                {"id": install_id}).rowcount
    return {"ok": True, "deleted": n}


def share(connection, payload: Any, *, ip: str = "") -> dict[str, Any]:
    from .core import make_id

    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise CollectorError(422, "unsupported_version")
    clean, _ = allow.sanitize(payload, allow.SHARE)
    if not clean.get("install_id") or not clean.get("bundle"):
        raise CollectorError(422, "invalid_share")
    if not IP_LIMIT.allow(ip_key(ip)) or not INSTALL_LIMIT.allow("share:" + clean["install_id"]):
        raise CollectorError(429, "rate_limited")
    sid = make_id("tmsh")
    connection.execute(text(
        "INSERT INTO tm_shares (id, install_id, body) VALUES (:id, :i, CAST(:b AS JSONB))"),
        {"id": sid, "i": clean["install_id"], "b": json.dumps(clean)})
    return {"ok": True, "id": sid}


# ------------------------------------------------------------------------------------------
# Retention
# ------------------------------------------------------------------------------------------


def retention(connection, *, now: Optional[datetime] = None) -> dict[str, int]:
    """Older than 18 months: roll into monthly counts, then delete."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=RETENTION_DAYS)).date()
    connection.execute(text("""
        INSERT INTO tm_rollups (month, metric, dims, value)
        SELECT date_trunc('month', day)::date, 'active_installs',
               COALESCE(edition, '') || '|' || COALESCE(version, ''), COUNT(DISTINCT install_id)
        FROM tm_heartbeats WHERE day < :c GROUP BY 1, 3
        ON CONFLICT (month, metric, dims) DO UPDATE SET value = GREATEST(tm_rollups.value, EXCLUDED.value)"""),
        {"c": cutoff})
    connection.execute(text("""
        INSERT INTO tm_rollups (month, metric, dims, value)
        SELECT date_trunc('month', day)::date, 'batches', '', COUNT(*)
        FROM tm_batches WHERE day < :c GROUP BY 1
        ON CONFLICT (month, metric, dims) DO UPDATE SET value = tm_rollups.value + EXCLUDED.value"""),
        {"c": cutoff})
    out = {}
    for table, col in (("tm_heartbeats", "day"), ("tm_batches", "day"),
                       ("tm_shares", "received_at")):
        out[table] = connection.execute(text(f"DELETE FROM {table} WHERE {col} < :c"),
                                        {"c": cutoff}).rowcount
    out["tm_installs"] = connection.execute(text(
        "DELETE FROM tm_installs WHERE last_seen_day < :c"), {"c": cutoff}).rowcount
    return out


# ------------------------------------------------------------------------------------------
# The dashboard (Observability › Installs)
# ------------------------------------------------------------------------------------------

#: Installs counted anywhere: never one marked suspicious (too many new ids from one address).
_OK = "(SELECT install_id FROM tm_installs WHERE NOT suspicious)"
_SEEN = (f"(SELECT install_id, day FROM tm_heartbeats WHERE install_id IN {_OK} "
         f"UNION SELECT install_id, day FROM tm_batches WHERE install_id IN {_OK})")


def _range_floor(label: str) -> int:
    return {"0": 0, "1": 1, "2-5": 2, "6-20": 6, "21-100": 21, "101-500": 101, "500+": 501}.get(label, 0)


def dashboard(connection) -> dict[str, Any]:
    today = _today()
    active = {}
    for name, days in (("daily", 1), ("weekly", 7), ("monthly", 30)):
        active[name] = int(connection.execute(text(
            f"SELECT COUNT(DISTINCT install_id) FROM {_SEEN} s WHERE day > :d"),
            {"d": today - timedelta(days=days)}).scalar() or 0)
    total = int(connection.execute(text("SELECT COUNT(*) FROM tm_installs WHERE NOT suspicious")).scalar() or 0)
    held = int(connection.execute(text("SELECT COUNT(*) FROM tm_installs WHERE suspicious")).scalar() or 0)
    new_weeks = [{"week": r[0].isoformat(), "n": int(r[1])} for r in connection.execute(text("""
        SELECT date_trunc('week', first_seen_day)::date AS w, COUNT(*) FROM tm_installs
        WHERE first_seen_day > :d AND NOT suspicious GROUP BY 1 ORDER BY 1"""), {"d": today - timedelta(weeks=12)}).all()]
    cohorts_raw = connection.execute(text(f"""
        WITH c AS (SELECT install_id, date_trunc('week', first_seen_day)::date AS cohort
                   FROM tm_installs WHERE first_seen_day > :d AND NOT suspicious)
        SELECT c.cohort, ((date_trunc('week', s.day)::date - c.cohort) / 7) AS k,
               COUNT(DISTINCT s.install_id) AS n
        FROM c JOIN {_SEEN} s ON s.install_id = c.install_id
        GROUP BY 1, 2 ORDER BY 1, 2"""), {"d": today - timedelta(weeks=8)}).all()
    cohorts: dict[str, list[int]] = {}
    for cohort, k, n in cohorts_raw:
        row = cohorts.setdefault(cohort.isoformat(), [0] * 8)
        if 0 <= int(k) < 8:
            row[int(k)] = int(n)
    cohort_rows = [{"week": w, "size": v[0], "pct": [round(100 * x / v[0]) if v[0] else 0 for x in v]}
                   for w, v in sorted(cohorts.items())]
    eligible = connection.execute(text(f"""
        SELECT COUNT(*) AS n,
               COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM {_SEEN} s WHERE s.install_id = i.install_id
                                AND s.day >= i.first_seen_day + 30)) AS kept
        FROM tm_installs i WHERE i.first_seen_day <= :d AND NOT i.suspicious"""),
        {"d": today - timedelta(days=30)}).mappings().first() or {}
    kept_pct = (round(100 * int(eligible["kept"] or 0) / int(eligible["n"]))
                if int(eligible.get("n") or 0) else None)

    def dist(column: str) -> list[dict[str, Any]]:
        return [{"key": r[0] or "unknown", "n": int(r[1])} for r in connection.execute(text(f"""
            SELECT {column}, COUNT(*) FROM tm_installs WHERE last_seen_day > :d AND NOT suspicious
            GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""), {"d": today - timedelta(days=30)}).all()]

    # Opted-in installs: each one's newest batch in 30 days for state, every batch for flows.
    latest = connection.execute(text(f"""
        SELECT DISTINCT ON (install_id) install_id, body FROM tm_batches WHERE day > :d
          AND install_id IN {_OK}
        ORDER BY install_id, day DESC"""), {"d": today - timedelta(days=30)}).mappings().all()
    flows = connection.execute(text(
        f"SELECT body FROM tm_batches WHERE day > :d AND install_id IN {_OK} ORDER BY day DESC LIMIT 5000"),
        {"d": today - timedelta(days=30)}).scalars().all()
    scale_dist: dict[str, collections.Counter] = {k: collections.Counter() for k in allow.SCALE_FIELDS}
    scale_floor: dict[str, int] = {k: 0 for k in allow.SCALE_FIELDS}
    funnel = collections.Counter()
    for row in latest:
        body = row["body"] if isinstance(row["body"], dict) else json.loads(row["body"])
        for k, v in (body.get("scale") or {}).items():
            if k in scale_dist:
                scale_dist[k][v] += 1
                scale_floor[k] += _range_floor(v)
        for step, bucket in ((body.get("usage") or {}).get("funnel") or {}).items():
            if bucket:
                funnel[step] += 1
    ratings = collections.Counter()
    chips = collections.Counter()
    errors = collections.Counter()
    checkin = collections.Counter()
    groups: dict[tuple[str, str], dict[str, float]] = {}
    for raw in flows:
        body = raw if isinstance(raw, dict) else json.loads(raw)
        fb = body.get("feedback") or {}
        for rating, n in ((fb.get("ratings") or {}).get("build") or {}).items():
            ratings[rating] += int(n)
        for subject in (fb.get("chips") or {}).values():
            for chip, n in subject.items():
                chips[chip] += int(n)
        for per_kind in (fb.get("rejects") or {}).values():
            for chip, n in per_kind.items():
                chips[chip] += int(n)
        for a, n in (fb.get("checkin") or {}).items():
            checkin[a] += int(n)
        for cls, n in ((body.get("health") or {}).get("errors") or {}).items():
            errors[cls] += int(n)
        cards = body.get("scorecards") or {}
        units = [dict(r, n=1, score_mean=r.get("score"), merged=1 if r.get("pr_merged") else 0)
                 for r in cards.get("rows") or []] + list(cards.get("agg") or [])
        for u in units:
            if u.get("unit_kind") not in ("build", "quick_change"):
                continue
            for dim in ("harness", "model", "role_key"):
                key = (dim, str(u.get(dim) or "unknown"))
                g = groups.setdefault(key, {"n": 0, "score_sum": 0.0, "scored": 0, "merged": 0})
                n = int(u.get("n") or 0)
                g["n"] += n
                g["merged"] += int(u.get("merged") or 0)
                if u.get("score_mean") is not None:
                    g["score_sum"] += float(u["score_mean"]) * n
                    g["scored"] += n
    rated = sum(ratings.values())
    by: dict[str, list[dict[str, Any]]] = {"harness": [], "model": [], "role_key": []}
    from .scorecards import HARNESS_NAMES

    roles = allow.builtin_role_names()

    def name_of(dim: str, key: str) -> str:
        if dim == "harness":
            return HARNESS_NAMES.get(key, key.replace("_", " ").title())
        if dim == "role_key":
            return roles.get(key) or key.replace("_", " ").title()
        return key

    for (dim, key), g in sorted(groups.items(), key=lambda kv: -kv[1]["n"]):
        by[dim].append({"key": key, "name": name_of(dim, key), "n": int(g["n"]),
                        "score": round(g["score_sum"] / g["scored"], 3) if g["scored"] else None,
                        "merged": round(g["merged"] / g["n"], 3) if g["n"] else None})
    m = active["monthly"]
    proof = (f"{m} active install{'' if m == 1 else 's'} this month"
             + (f", {kept_pct}% still active after 30 days" if kept_pct is not None else ""))
    return {
        "proof": proof,
        "active": active, "total_installs": total, "kept_30d_pct": kept_pct, "held_back": held,
        "new_per_week": new_weeks, "cohorts": cohort_rows,
        "versions": dist("version"), "editions": dist("edition"), "os": dist("os"),
        "arch": dist("arch"), "methods": dist("install_method"),
        "opted_in": len(latest),
        "scale": {k: {"at_least": scale_floor[k],
                      "dist": [{"key": r, "n": scale_dist[k][r]} for r in allow.COUNT_RANGES
                               if scale_dist[k][r]]} for k in allow.SCALE_FIELDS},
        "ship": {"rated": rated, "yes": ratings["yes"], "with_changes": ratings["with_changes"],
                 "no": ratings["no"],
                 "rate": round(ratings["yes"] / rated, 3) if rated else None},
        "chips": [{"key": k, "n": n} for k, n in chips.most_common(12)],
        "funnel": [{"key": k, "n": funnel[k]} for k in allow.FUNNEL],
        "checkin": [{"key": k, "n": checkin[k]} for k in allow.PMF_ANSWERS],
        "by": {k: v[:12] for k, v in by.items()},
        "errors": [{"key": k, "n": n} for k, n in errors.most_common(12)],
        "installer": None,  # installer step failures are not reported (install.sh is unchanged)
    }


def csv_export(connection) -> str:
    view = dashboard(connection)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["section", "key", "value"])
    w.writerow(["proof", "", view["proof"]])
    for k, v in view["active"].items():
        w.writerow(["active", k, v])
    w.writerow(["installs", "total", view["total_installs"]])
    w.writerow(["installs", "kept_30d_pct", view["kept_30d_pct"]])
    for r in view["new_per_week"]:
        w.writerow(["new_per_week", r["week"], r["n"]])
    for c in view["cohorts"]:
        w.writerow(["cohort", c["week"], " ".join(str(p) for p in c["pct"])])
    for section in ("versions", "editions", "os", "arch", "methods", "chips", "funnel", "errors",
                    "checkin"):
        for r in view[section]:
            w.writerow([section, r["key"], r["n"]])
    for k, v in view["scale"].items():
        w.writerow(["scale_at_least", k, v["at_least"]])
    for dim, rows in view["by"].items():
        for r in rows:
            w.writerow([f"scorecards_by_{dim}", r["key"], f"n={r['n']} score={r['score']} merged={r['merged']}"])
    w.writerow(["ship", "rate", view["ship"]["rate"]])
    return out.getvalue()
