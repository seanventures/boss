import { randomUUID } from "node:crypto";
import express from "express";

import { detectIncidentForTicket, incidentInfoForTickets } from "../incidents/service.mjs";
import { withSla, SLA_DURATIONS_MS } from "../sla.mjs";

const VALID_PRIORITIES = new Set(Object.keys(SLA_DURATIONS_MS));
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function isStaff(roles) {
  return roles.includes("agent") || roles.includes("admin");
}

// Reserved author id for automated incident-resolution replies, seeded in db.mjs migration.
const SYSTEM_AUTHOR_ID = "system";

export async function repliesForTicket(pool, ticketId, metrics) {
  const { rows } = await pool.query(
    `SELECT r.id, r.ticket_id, r.author_id, r.body, r.created_at, u.name AS author_name, u.email AS author_email
     FROM ticket_replies r LEFT JOIN users u ON u.id = r.author_id
     WHERE r.ticket_id = $1 ORDER BY r.created_at ASC`,
    [ticketId],
  );
  const replies = [];
  for (const r of rows) {
    if (!r.author_id || !r.body || !r.created_at) {
      metrics?.ticketReplySkippedTotal.inc();
      continue;
    }
    const isAutomated = r.author_id === SYSTEM_AUTHOR_ID;
    replies.push({
      id: r.id,
      ticket_id: r.ticket_id,
      author_id: r.author_id,
      author_name: isAutomated ? "HelpDesk AI (automated)" : r.author_name || r.author_email,
      is_automated: isAutomated,
      body: r.body,
      created_at: r.created_at,
    });
  }
  return replies;
}

const TAG_RE = /^[a-z0-9_-]{1,20}$/;

function tagsEnabled() {
  return process.env.TICKET_TAGS_ENABLED !== "false";
}

function normalizeTag(raw) {
  return typeof raw === "string" ? raw.trim().toLowerCase() : null;
}

function recordTagEvent(pool, metrics, { ticketId, tag, action, actorId }) {
  pool
    .query(`INSERT INTO ticket_tag_events (id, ticket_id, tag, action, actor_id) VALUES ($1, $2, $3, $4, $5)`, [
      randomUUID(),
      ticketId,
      tag,
      action,
      actorId,
    ])
    .catch(() => metrics?.tagAuditLogFailuresTotal.inc());
}

async function attachTagsSingle(pool, ticket) {
  if (!ticket || !tagsEnabled()) return ticket;
  const { rows } = await pool.query(`SELECT tag FROM ticket_tags WHERE ticket_id = $1 ORDER BY tag`, [ticket.id]);
  return { ...ticket, tags: rows.map((r) => r.tag) };
}

async function attachIncidentSingle(pool, ticket, isStaffView) {
  if (!ticket) return ticket;
  const info = (await incidentInfoForTickets(pool, [ticket.id])).get(ticket.id);
  if (!info) return ticket;
  return { ...ticket, incident: isStaffView ? { status: info.status, groupId: info.groupId } : { status: info.status } };
}

async function attachIncidentMany(pool, tickets, isStaffView) {
  if (tickets.length === 0) return tickets;
  const map = await incidentInfoForTickets(pool, tickets.map((t) => t.id));
  if (map.size === 0) return tickets;
  return tickets.map((t) => {
    const info = map.get(t.id);
    if (!info) return t;
    return { ...t, incident: isStaffView ? { status: info.status, groupId: info.groupId } : { status: info.status } };
  });
}

async function attachTagsMany(pool, tickets) {
  if (!tagsEnabled() || tickets.length === 0) return tickets;
  const ids = tickets.map((t) => t.id);
  const { rows } = await pool.query(
    `SELECT ticket_id, tag FROM ticket_tags WHERE ticket_id = ANY($1) ORDER BY tag`,
    [ids],
  );
  const byTicket = new Map();
  for (const row of rows) {
    if (!byTicket.has(row.ticket_id)) byTicket.set(row.ticket_id, []);
    byTicket.get(row.ticket_id).push(row.tag);
  }
  return tickets.map((t) => ({ ...t, tags: byTicket.get(t.id) || [] }));
}

export function createTicketsRouter({ pool, requireUser, requireRole, suggest, metrics }) {
  const router = express.Router();
  router.use(express.json());

  router.get("/api/tickets", requireUser, async (req, res, next) => {
    try {
      const staffView = isStaff(req.roles);
      const { rows } = staffView
        ? await pool.query(`SELECT * FROM tickets ORDER BY created_at DESC`)
        : await pool.query(`SELECT * FROM tickets WHERE customer_id = $1 ORDER BY created_at DESC`, [req.user.id]);
      const withTags = await attachTagsMany(pool, rows);
      const withIncidents = await attachIncidentMany(pool, withTags, staffView);
      const now = new Date();
      let withSlaTickets = withIncidents.map((t) => withSla(t, now, metrics));

      const filter = typeof req.query.filter === "string" ? req.query.filter : "";
      if (filter === "breached") {
        withSlaTickets = withSlaTickets.filter((t) => t.status === "open" && t.sla.state === "red");
        metrics?.ticketBreachedFilterRequestsTotal.inc({ result: "ok" });
      }

      res.json({ tickets: withSlaTickets });
    } catch (err) {
      next(err);
    }
  });

  router.post("/api/tickets", requireUser, async (req, res, next) => {
    try {
      const { subject, body } = req.body || {};
      if (typeof subject !== "string" || !subject.trim() || typeof body !== "string" || !body.trim()) {
        return res.status(400).json({ error: "invalid_ticket" });
      }
      const id = randomUUID();
      // priority defaults to "normal" at creation time (not left NULL) so a reader never races
      // the fire-and-forget suggestion below: the suggestion still overwrites it once resolved,
      // but until then the ticket already carries its real default rather than an unset value.
      const { rows } = await pool.query(
        `INSERT INTO tickets (id, customer_id, subject, body, priority) VALUES ($1, $2, $3, $4, 'normal') RETURNING *`,
        [id, req.user.id, subject.trim(), body.trim()],
      );
      const created = rows[0];
      await detectIncidentForTicket(pool, created, { metrics });
      const withTags = await attachTagsSingle(pool, created);
      res.json(await attachIncidentSingle(pool, withTags, isStaff(req.roles)));
      metrics?.ticketsCreated.inc();

      // Fire-and-forget: a slow or failing suggestion must never block or fail ticket creation.
      Promise.resolve()
        .then(() => suggest({ subject, body }))
        .then((suggestion) =>
          pool.query(
            `UPDATE tickets SET category = $2, priority = $3, draft_reply = $4, suggestion_status = 'ready' WHERE id = $1`,
            [id, suggestion.category, suggestion.priority, suggestion.draftReply],
          ),
        )
        .then(() => metrics?.suggestionsCompleted.inc({ result: "success" }))
        .catch(() => {
          metrics?.suggestionsCompleted.inc({ result: "failure" });
          return pool.query(`UPDATE tickets SET suggestion_status = 'unavailable' WHERE id = $1`, [id]).catch(() => {});
        });
    } catch (err) {
      next(err);
    }
  });

  router.get("/api/tickets/:id", requireUser, async (req, res, next) => {
    try {
      const { rows } = await pool.query(`SELECT * FROM tickets WHERE id = $1`, [req.params.id]);
      const ticket = rows[0];
      if (!ticket) return res.status(404).json({ error: "not_found" });
      if (!isStaff(req.roles) && ticket.customer_id !== req.user.id) {
        return res.status(403).json({ error: "forbidden" });
      }
      const withTags = await attachTagsSingle(pool, ticket);
      const withIncident = await attachIncidentSingle(pool, withTags, isStaff(req.roles));
      const replies = await repliesForTicket(pool, ticket.id, metrics);
      metrics?.ticketReplyCount.observe(replies.length);
      if (withIncident.incident && withIncident.incident.status !== "open") {
        metrics?.incidentBannerServedTotal.inc({ status: withIncident.incident.status });
      }
      res.json({ ...withSla(withIncident, new Date(), metrics), replies });
    } catch (err) {
      next(err);
    }
  });

  router.post("/api/tickets/:id/replies", requireUser, async (req, res, next) => {
    try {
      const { body } = req.body || {};
      if (typeof body !== "string" || !body.trim()) return res.status(400).json({ error: "invalid_reply" });
      const { rows } = await pool.query(`SELECT * FROM tickets WHERE id = $1`, [req.params.id]);
      const ticket = rows[0];
      if (!ticket) return res.status(404).json({ error: "not_found" });
      if (!isStaff(req.roles) && ticket.customer_id !== req.user.id) {
        return res.status(403).json({ error: "forbidden" });
      }
      const id = randomUUID();
      const { rows: replyRows } = await pool.query(
        `INSERT INTO ticket_replies (id, ticket_id, author_id, body) VALUES ($1, $2, $3, $4) RETURNING *`,
        [id, ticket.id, req.user.id, body.trim()],
      );
      res.json(replyRows[0]);
    } catch (err) {
      next(err);
    }
  });

  router.get("/api/tickets/:id/replies", requireUser, async (req, res, next) => {
    try {
      const { rows } = await pool.query(`SELECT * FROM tickets WHERE id = $1`, [req.params.id]);
      const ticket = rows[0];
      if (!ticket) return res.status(404).json({ error: "not_found" });
      if (!isStaff(req.roles) && ticket.customer_id !== req.user.id) {
        return res.status(403).json({ error: "forbidden" });
      }
      const { rows: replyRows } = await pool.query(
        `SELECT * FROM ticket_replies WHERE ticket_id = $1 ORDER BY created_at ASC`,
        [req.params.id],
      );
      res.json({ replies: replyRows });
    } catch (err) {
      next(err);
    }
  });

  router.post("/api/tickets/:id/close", requireRole("agent"), async (req, res, next) => {
    try {
      const { rows } = await pool.query(
        `UPDATE tickets SET status = 'closed', closed_at = CURRENT_TIMESTAMP WHERE id = $1 RETURNING *`,
        [req.params.id],
      );
      if (!rows[0]) return res.status(404).json({ error: "not_found" });
      metrics?.ticketsClosed.inc();
      res.json(rows[0]);
    } catch (err) {
      next(err);
    }
  });

  router.post("/api/tickets/:id/tags", requireRole("agent"), async (req, res, next) => {
    try {
      if (!tagsEnabled()) return res.status(404).json({ error: "not_found" });
      const { tags } = req.body || {};
      if (
        !Array.isArray(tags) ||
        tags.length === 0 ||
        !tags.every((t) => typeof t === "string" && TAG_RE.test(t.trim().toLowerCase()))
      ) {
        return res.status(400).json({ error: "invalid_tags" });
      }
      const normalized = [...new Set(tags.map((t) => normalizeTag(t)))];
      if (normalized.length > 5) {
        return res.status(409).json({ error: "tag_limit" });
      }

      const { rows: ticketRows } = await pool.query(`SELECT id FROM tickets WHERE id = $1`, [req.params.id]);
      if (!ticketRows[0]) return res.status(404).json({ error: "not_found" });

      const { rows: existingRows } = await pool.query(`SELECT tag FROM ticket_tags WHERE ticket_id = $1`, [
        req.params.id,
      ]);
      const existingTags = existingRows.map((r) => r.tag);

      const toAdd = normalized.filter((tag) => !existingTags.includes(tag));
      const toRemove = existingTags.filter((tag) => !normalized.includes(tag));

      const client = await pool.connect();
      try {
        await client.query("BEGIN");
        for (const tag of toRemove) {
          await client.query(`DELETE FROM ticket_tags WHERE ticket_id = $1 AND tag = $2`, [req.params.id, tag]);
        }
        for (const tag of toAdd) {
          await client.query(`INSERT INTO ticket_tags (ticket_id, tag, created_by) VALUES ($1, $2, $3)`, [
            req.params.id,
            tag,
            req.user.id,
          ]);
        }
        await client.query("COMMIT");
      } catch (err) {
        await client.query("ROLLBACK");
        throw err;
      } finally {
        client.release();
      }

      metrics?.tagWriteTotal.inc();
      metrics?.tagsPerTicket.observe(normalized.length);
      for (const tag of toAdd) {
        recordTagEvent(pool, metrics, { ticketId: req.params.id, tag, action: "add", actorId: req.user.id });
      }
      for (const tag of toRemove) {
        recordTagEvent(pool, metrics, { ticketId: req.params.id, tag, action: "remove", actorId: req.user.id });
      }

      const { rows: ticketResultRows } = await pool.query(`SELECT * FROM tickets WHERE id = $1`, [req.params.id]);
      res.json(await attachTagsSingle(pool, ticketResultRows[0]));
    } catch (err) {
      next(err);
    }
  });

  router.delete("/api/tickets/:id/tags/:tag", requireRole("agent"), async (req, res, next) => {
    try {
      if (!tagsEnabled()) return res.status(404).json({ error: "not_found" });
      const tag = normalizeTag(req.params.tag);
      if (!tag || !TAG_RE.test(tag)) return res.status(400).json({ error: "invalid_tags" });

      const { rows: ticketRows } = await pool.query(`SELECT * FROM tickets WHERE id = $1`, [req.params.id]);
      if (!ticketRows[0]) return res.status(404).json({ error: "not_found" });

      const { rowCount } = await pool.query(`DELETE FROM ticket_tags WHERE ticket_id = $1 AND tag = $2`, [
        req.params.id,
        tag,
      ]);
      // Only audit an actual removal; deleting an absent tag is an idempotent no-op.
      if (rowCount > 0) {
        recordTagEvent(pool, metrics, { ticketId: req.params.id, tag, action: "remove", actorId: req.user.id });
      }

      const { rows: tagRows } = await pool.query(`SELECT tag FROM ticket_tags WHERE ticket_id = $1 ORDER BY tag`, [
        req.params.id,
      ]);
      metrics?.tagWriteTotal.inc();
      metrics?.tagsPerTicket.observe(tagRows.length);
      res.json({ ...ticketRows[0], tags: tagRows.map((r) => r.tag) });
    } catch (err) {
      next(err);
    }
  });

  router.patch("/api/tickets/:id/priority", requireUser, async (req, res, next) => {
    try {
      if (!isStaff(req.roles)) return res.status(403).json({ error: "forbidden" });
      const { priority } = req.body || {};
      if (typeof priority !== "string" || !VALID_PRIORITIES.has(priority)) {
        return res.status(400).json({ error: "invalid_priority" });
      }
      // A malformed id must be a 4xx, not a Postgres uuid-cast 500.
      if (typeof req.params.id !== "string" || !UUID_RE.test(req.params.id)) {
        metrics?.ticketPriorityChangedTotal.inc({ result: "failure" });
        return res.status(404).json({ error: "not_found" });
      }
      const { rows } = await pool.query(
        `WITH old AS (SELECT priority AS old_priority FROM tickets WHERE id = $1)
         UPDATE tickets t SET priority = $2
         FROM old
         WHERE t.id = $1
         RETURNING t.*, old.old_priority`,
        [req.params.id, priority],
      );
      const row = rows[0];
      if (!row) {
        metrics?.ticketPriorityChangedTotal.inc({ result: "failure" });
        return res.status(404).json({ error: "not_found" });
      }
      const oldPriority = row.old_priority;
      delete row.old_priority;
      console.log(
        JSON.stringify({
          event: "ticket_priority_changed",
          ticket_id: row.id,
          actor_id: req.user.id,
          old_priority: oldPriority,
          new_priority: priority,
          timestamp: new Date().toISOString(),
        }),
      );
      metrics?.ticketPriorityChangedTotal.inc({ result: "success" });
      const withTags = await attachTagsSingle(pool, row);
      const withIncident = await attachIncidentSingle(pool, withTags, true);
      res.json(withSla(withIncident, new Date(), metrics));
    } catch (err) {
      metrics?.ticketPriorityChangedTotal.inc({ result: "failure" });
      next(err);
    }
  });

  router.get("/api/triage/queue", requireRole("agent"), async (req, res, next) => {
    try {
      const tag = tagsEnabled() && typeof req.query.tag === "string" ? req.query.tag.trim().toLowerCase() : "";
      const { rows } = tag
        ? await pool.query(
            `SELECT t.* FROM tickets t JOIN ticket_tags tt ON tt.ticket_id = t.id AND tt.tag = $1
             WHERE t.status = 'open' ORDER BY t.created_at ASC`,
            [tag],
          )
        : await pool.query(`SELECT * FROM tickets WHERE status = 'open' ORDER BY created_at ASC`);
      if (tag) metrics?.queueTagFilterRequestsTotal.inc();
      const withTags = await attachTagsMany(pool, rows);
      res.json({ tickets: await attachIncidentMany(pool, withTags, true) });
    } catch (err) {
      next(err);
    }
  });

  return { router };
}
