import path from "node:path";
import { fileURLToPath } from "node:url";
import express, { type Express, type NextFunction, type Request, type Response } from "express";
import { mountAuth } from "./auth";
import { getPool } from "./db";
import { createHealthRouter } from "./routes/health";
import { createTicketsRouter } from "./routes/tickets";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export async function createApp(): Promise<Express> {
  const pool = getPool();
  const app = express();
  app.disable("x-powered-by");
  app.set("trust proxy", process.env.TRUST_PROXY || "loopback");
  app.use(express.json());

  const auth = await mountAuth();
  app.use(auth.router);
  app.use(createHealthRouter(pool));
  app.use(createTicketsRouter(pool, auth));
  app.use(express.static(path.join(__dirname, "..", "..", "public")));

  app.use((err: unknown, _req: Request, res: Response, _next: NextFunction) => {
    console.error(err);
    if (res.headersSent) return;
    res.status(500).json({ error: "internal_error" });
  });
  return app;
}
