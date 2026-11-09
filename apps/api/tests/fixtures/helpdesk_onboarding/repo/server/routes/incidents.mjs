import { randomUUID } from "node:crypto";
import express from "express";

import {
  constantTimeEqual,
  dismissIncidentGroup,
  isAllowedFixUrl,
  listOpenSignals,
  listTriageIncidents,
  verifyCallbackSignature,
} from "../incidents/service.mjs";

// System author for automated incident-resolution replies, seeded in db.mjs migration.
const SYSTEM_AUTHOR_ID = "system";

export function createIncidentsRouter({ pool, requireRole, metrics }) {
  const router = express.Router();

  // BOSS polls this every 30s with a bearer token it was given out-of-band.
  router.get("/boss/signals", async (req, res, next) => {
    try {
      const token = process.env.BOSS_SIGNALS_TOKEN || "";
      const header = req.get("authorization") || "";
      const provided = header.startsWith("Bearer ") ? header.slice(7) : "";
      if (!token || !constantTimeEqual(provided, token)) {
        metrics?.signalsRequestsTotal.inc({ result: "unauthorized" });
        return res.status(401).json({ error: "unauthorized" });
      }
      const signals = await listOpenSignals(pool);
      metrics?.signalsRequestsTotal.inc({ result: "success" });
      res.json({ signals });
    } catch (err) {
      next(err);
    }
  });

  // BOSS's signed callback. Raw body is required to verify the HMAC before any JSON parsing
  // or ticket mutation happens.
  router.post("/boss/incidents/:groupId", express.raw({ type: "*/*", limit: "256kb" }), async (req, res, next) => {
    try {
      const token = process.env.BOSS_SIGNALS_TOKEN || "";
      const rawBody = Buffer.isBuffer(req.body) ? req.body.toString("utf8") : "";
      const verdict = verifyCallbackSignature({
        token,
        timestampHeader: req.get("x-boss-timestamp"),
        rawBody,
        signatureHeader: req.get("x-boss-signature"),
      });
      if (!verdict.ok) {
        metrics?.signalsCallbackTotal.inc({ result: verdict.reason === "stale" ? "stale" : "bad_signature" });
        return res.status(401).json({ error: verdict.reason });
      }

      let payload;
      try {
        payload = rawBody ? JSON.parse(rawBody) : {};
      } catch {
        metrics?.signalsCallbackTotal.inc({ result: "bad_body" });
        return res.status(400).json({ error: "invalid_body" });
      }

      const groupId = req.params.groupId;
      const { rows } = await pool.query(`SELECT * FROM incident_groups WHERE id = $1`, [groupId]);
      const group = rows[0];
      if (!group) {
        metrics?.signalsCallbackTotal.inc({ result: "not_found" });
        return res.status(404).json({ error: "not_found" });
      }

      const eventId = `${payload.status || ""}:${payload.resolved_at || req.get("x-boss-timestamp")}`;
      if (group.last_applied_event_id === eventId) {
        metrics?.signalsCallbackTotal.inc({ result: "duplicate" });
        return res.status(200).json({ ok: true, duplicate: true });
      }

      const { rows: ticketRows } = await pool.query(`SELECT ticket_id FROM incident_group_tickets WHERE group_id = $1`, [
        groupId,
      ]);
      const ticketIds = ticketRows.map((r) => r.ticket_id);

      if (payload.status === "investigating") {
        await pool.query(`UPDATE incident_groups SET status = 'investigating', last_applied_event_id = $2 WHERE id = $1`, [
          groupId,
          eventId,
        ]);
        metrics?.signalsCallbackTotal.inc({ result: "investigating" });
        return res.status(200).json({ ok: true });
      }

      if (payload.status === "resolved") {
        const fixUrl = payload.pull_request_url || null;
        const fixUrlAllowed = fixUrl ? isAllowedFixUrl(fixUrl) : true;
        const replyBody = fixUrl && fixUrlAllowed
          ? `Known incident resolved.\nRoot cause: ${payload.root_cause || "unspecified"}\nFix: ${fixUrl}`
          : `Known incident resolved.\nRoot cause: ${payload.root_cause || "unspecified"}`;

        const client = await pool.connect();
        try {
          await client.query("BEGIN");
          for (const ticketId of ticketIds) {
            await client.query(
              `INSERT INTO ticket_replies (id, ticket_id, author_id, body) VALUES ($1, $2, $3, $4)`,
              [randomUUID(), ticketId, SYSTEM_AUTHOR_ID, replyBody],
            );
            await client.query(
              `UPDATE tickets SET status = 'closed', closed_at = CURRENT_TIMESTAMP,
                      flagged_for_review = flagged_for_review OR $2
               WHERE id = $1`,
              [ticketId, Boolean(fixUrl && !fixUrlAllowed)],
            );
          }
          await client.query(
            `UPDATE incident_groups
             SET status = 'resolved', root_cause = $2, fix_url = $3, last_applied_event_id = $4
             WHERE id = $1`,
            [groupId, payload.root_cause || null, fixUrlAllowed ? fixUrl : null, eventId],
          );
          await client.query("COMMIT");
        } catch (err) {
          await client.query("ROLLBACK");
          throw err;
        } finally {
          client.release();
        }
        metrics?.signalsCallbackTotal.inc({ result: "resolved" });
        return res.status(200).json({ ok: true });
      }

      metrics?.signalsCallbackTotal.inc({ result: "unknown_status" });
      return res.status(400).json({ error: "unknown_status" });
    } catch (err) {
      next(err);
    }
  });

  // Scoped to these two paths only — an unscoped router.use(express.json()) would consume the
  // request body for every request that falls through this router unmatched (e.g. POST
  // /api/tickets), including the raw bytes the callback route above needs intact.
  const jsonBody = express.json();

  router.get("/api/incidents", jsonBody, requireRole("agent"), async (req, res, next) => {
    try {
      res.json({ incidents: await listTriageIncidents(pool) });
    } catch (err) {
      next(err);
    }
  });

  router.post("/api/incidents/:id/dismiss", jsonBody, requireRole("agent"), async (req, res, next) => {
    try {
      const dismissed = await dismissIncidentGroup(pool, req.params.id);
      if (!dismissed) return res.status(404).json({ error: "not_found" });
      res.json({ ok: true });
    } catch (err) {
      next(err);
    }
  });

  return { router };
}
