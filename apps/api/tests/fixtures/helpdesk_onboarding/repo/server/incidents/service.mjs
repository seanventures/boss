import { randomUUID, timingSafeEqual, createHmac } from "node:crypto";

import { matchSymptom, planGrouping, WINDOW_MS } from "./matching.mjs";
import { incidentDetectionEnabled } from "../flags.mjs";

export function constantTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || !a || !b) return false;
  const x = Buffer.from(a);
  const y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}

// Allowlist for a fix link BOSS posts back on resolution — never render an arbitrary URL
// straight onto a customer-facing ticket.
const FIX_URL_ALLOWED_HOSTS = (process.env.FIX_URL_ALLOWED_HOSTS || "github.com")
  .split(",")
  .map((s) => s.trim().toLowerCase())
  .filter(Boolean);

export function isAllowedFixUrl(url) {
  if (typeof url !== "string" || !url) return false;
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:") return false;
    return FIX_URL_ALLOWED_HOSTS.some((host) => parsed.hostname === host || parsed.hostname.endsWith(`.${host}`));
  } catch {
    return false;
  }
}

// Runs at ticket-create time. Fail-open and bounded: any error or timeout is swallowed (after
// recording a metric) so ticket creation is never affected by detection.
export async function detectIncidentForTicket(pool, ticket, { metrics, timeoutMs = 1500 } = {}) {
  if (!incidentDetectionEnabled()) return null;
  try {
    return await withTimeout(detectIncidentForTicketInner(pool, ticket), timeoutMs);
  } catch (err) {
    metrics?.incidentDetectionErrorsTotal?.inc({ stage: err?.message === "timeout" ? "timeout" : "query" });
    return null;
  }
}

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timeout")), ms);
    promise.then(
      (v) => {
        clearTimeout(timer);
        resolve(v);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

async function detectIncidentForTicketInner(pool, ticket) {
  const match = matchSymptom(ticket.subject, ticket.body);
  if (!match) return null;
  const newCreatedAt = new Date(ticket.created_at).getTime();

  const { rows: groupRows } = await pool.query(
    `SELECT id, (extract(epoch FROM first_seen) * 1000) AS first_seen_ms
     FROM incident_groups
     WHERE product = $1 AND status IN ('open', 'investigating')
     ORDER BY first_seen ASC`,
    [match.product],
  );
  const existingGroups = groupRows.map((r) => ({ id: r.id, firstSeen: Number(r.first_seen_ms) }));

  const windowStartDate = new Date(newCreatedAt - WINDOW_MS);
  const { rows: candidateRows } = await pool.query(
    `SELECT id, subject, body, (extract(epoch FROM created_at) * 1000) AS created_at_ms
     FROM tickets
     WHERE status = 'open' AND created_at >= $1
     ORDER BY created_at ASC`,
    [windowStartDate],
  );
  const candidates = candidateRows
    .filter((r) => r.id !== ticket.id)
    .filter((r) => {
      const m = matchSymptom(r.subject, r.body);
      return m && m.product === match.product;
    })
    .map((r) => ({ ticketId: r.id, createdAt: Number(r.created_at_ms) }));
  candidates.push({ ticketId: ticket.id, createdAt: newCreatedAt });
  candidates.sort((a, b) => a.createdAt - b.createdAt);

  const plan = planGrouping({ existingGroups, candidates });
  if (plan.action === "none") return null;

  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    let groupId;
    if (plan.action === "create") {
      groupId = randomUUID();
      await client.query(
        `INSERT INTO incident_groups (id, product, symptom_key, title, status, first_seen, last_seen)
         VALUES ($1, $2, $3, $4, 'open', to_timestamp($5 / 1000.0), to_timestamp($6 / 1000.0))`,
        [groupId, match.product, match.symptomKey, match.title, plan.firstSeen, plan.lastSeen],
      );
    } else {
      groupId = plan.groupId;
      await client.query(
        `UPDATE incident_groups SET last_seen = GREATEST(last_seen, to_timestamp($2 / 1000.0)) WHERE id = $1`,
        [groupId, newCreatedAt],
      );
    }
    for (const ticketId of plan.ticketIds) {
      await client.query(
        `INSERT INTO incident_group_tickets (group_id, ticket_id) VALUES ($1, $2) ON CONFLICT DO NOTHING`,
        [groupId, ticketId],
      );
    }
    await client.query("COMMIT");
    return { groupId, action: plan.action };
  } catch (err) {
    await client.query("ROLLBACK");
    throw err;
  } finally {
    client.release();
  }
}

// Open suspected incidents, content-minimized for BOSS: never subject/body text.
export async function listOpenSignals(pool) {
  const { rows } = await pool.query(
    `SELECT g.id, g.title, g.product, g.first_seen, g.last_seen,
            COALESCE(array_agg(t.ticket_id) FILTER (WHERE t.ticket_id IS NOT NULL), '{}') AS refs
     FROM incident_groups g
     LEFT JOIN incident_group_tickets t ON t.group_id = g.id
     WHERE g.status IN ('open', 'investigating')
     GROUP BY g.id
     ORDER BY g.first_seen ASC`,
  );
  return rows.map((g) => ({
    id: g.id,
    title: g.title,
    summary: g.title,
    affects: g.product,
    severity: "warning",
    refs: g.refs,
    count: g.refs.length,
    first_seen: g.first_seen.toISOString(),
    last_seen: g.last_seen.toISOString(),
  }));
}

// Agent-facing triage list: same groups, with a little more detail (product may be labeled
// "inferred" by the caller since it comes from keyword matching rather than a real field).
export async function listTriageIncidents(pool) {
  const { rows } = await pool.query(
    `SELECT g.id, g.title, g.product, g.status, g.first_seen, g.last_seen,
            COALESCE(array_agg(t.ticket_id) FILTER (WHERE t.ticket_id IS NOT NULL), '{}') AS refs
     FROM incident_groups g
     LEFT JOIN incident_group_tickets t ON t.group_id = g.id
     WHERE g.status IN ('open', 'investigating')
     GROUP BY g.id
     ORDER BY g.first_seen ASC`,
  );
  return rows.map((g) => ({
    id: g.id,
    title: g.title,
    product: g.product,
    productInferred: true,
    status: g.status,
    ticketIds: g.refs,
    count: g.refs.length,
    oldest: g.first_seen.toISOString(),
  }));
}

export async function dismissIncidentGroup(pool, groupId) {
  const { rows } = await pool.query(
    `UPDATE incident_groups SET status = 'dismissed' WHERE id = $1 AND status IN ('open', 'investigating') RETURNING id`,
    [groupId],
  );
  return rows.length > 0;
}

// Status banner info to attach to a ticket response. Customer view gets status text only;
// callers decide whether to include the agent-only group id.
export async function incidentInfoForTickets(pool, ticketIds) {
  if (ticketIds.length === 0) return new Map();
  const { rows } = await pool.query(
    `SELECT t.ticket_id, g.id AS group_id, g.status
     FROM incident_group_tickets t
     JOIN incident_groups g ON g.id = t.group_id
     WHERE t.ticket_id = ANY($1) AND g.status IN ('open', 'investigating', 'resolved')`,
    [ticketIds],
  );
  const byTicket = new Map();
  for (const row of rows) {
    byTicket.set(row.ticket_id, { groupId: row.group_id, status: row.status });
  }
  return byTicket;
}

export function verifyCallbackSignature({ token, timestampHeader, rawBody, signatureHeader, maxAgeSeconds = 300 }) {
  if (!token || !timestampHeader || !signatureHeader) return { ok: false, reason: "missing" };
  const timestamp = Number(timestampHeader);
  if (!Number.isFinite(timestamp)) return { ok: false, reason: "bad_timestamp" };
  const nowSeconds = Math.floor(Date.now() / 1000);
  if (Math.abs(nowSeconds - timestamp) > maxAgeSeconds) return { ok: false, reason: "stale" };

  const expected = `v1=${createHmac("sha256", token).update(`${timestampHeader}.${rawBody}`).digest("hex")}`;
  if (!constantTimeEqual(signatureHeader, expected)) return { ok: false, reason: "bad_signature" };
  return { ok: true };
}
