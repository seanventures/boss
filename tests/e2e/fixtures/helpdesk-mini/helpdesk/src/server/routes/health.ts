import { Router } from "express";
import type pg from "pg";

export function createHealthRouter(pool: pg.Pool): Router {
  const router = Router();
  router.get("/healthz", (_req, res) => {
    res.status(200).json({ ok: true });
  });
  router.get("/readyz", async (_req, res) => {
    try {
      const migrated = await pool.query("SELECT to_regclass('public.tickets') IS NOT NULL AS ok");
      res.status(migrated.rows[0]?.ok ? 200 : 503).json({ ok: Boolean(migrated.rows[0]?.ok) });
    } catch {
      res.status(503).json({ ok: false });
    }
  });
  return router;
}
