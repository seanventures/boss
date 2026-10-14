import { Router } from "express";
import type pg from "pg";
import type { AuthHandle } from "../auth";
import { classifyByKeyword } from "../classify";

export function createTicketsRouter(pool: pg.Pool, auth: AuthHandle): Router {
  const router = Router();
  router.get("/api/tickets", auth.requireUser, async (_req, res, next) => {
    try {
      const { rows } = await pool.query(
        "SELECT id, title, category, status FROM tickets ORDER BY id DESC LIMIT 50",
      );
      res.json({ tickets: rows });
    } catch (err) {
      next(err);
    }
  });
  router.post("/api/tickets", auth.requireUser, async (req, res, next) => {
    const title = String(req.body?.title ?? "").trim();
    const description = String(req.body?.description ?? "");
    if (!title) {
      res.status(400).json({ error: "title_required" });
      return;
    }
    try {
      const { category } = classifyByKeyword(`${title} ${description}`);
      const { rows } = await pool.query(
        "INSERT INTO tickets (title, description, category) VALUES ($1, $2, $3) RETURNING id, title, category, status",
        [title, description, category],
      );
      res.status(201).json(rows[0]);
    } catch (err) {
      next(err);
    }
  });
  return router;
}
