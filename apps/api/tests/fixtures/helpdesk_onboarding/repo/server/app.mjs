import path from "node:path";
import { fileURLToPath } from "node:url";
import express from "express";

import { createAuth } from "./auth/routes.mjs";
import { PgUserStore } from "./auth/pgUserStore.mjs";
import { createSessionStore } from "./auth/sessionStore.mjs";
import { createPasswordAuth } from "./auth/password.mjs";
import { createTicketsRouter } from "./routes/tickets.mjs";
import { createAvatarRouter } from "./routes/avatar.mjs";
import { createFlagsRouter } from "./routes/flags.mjs";
import { createIncidentsRouter } from "./routes/incidents.mjs";
import { createMetrics } from "./metrics.mjs";
import { suggestForTicket } from "./ai/suggest.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIST = path.join(__dirname, "..", "web", "dist");

// AUTH_AGENT_EMAILS follows the exact allow-list discipline config.mjs already applies to
// AUTH_ADMIN_EMAILS; read here (app-side) rather than editing the connector's config.mjs.
function agentEmailsFromEnv(env = process.env) {
  return String(env.AUTH_AGENT_EMAILS || "")
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
}

export function createApp({ config, pool }) {
  const app = express();
  app.disable("x-powered-by");
  app.set("trust proxy", process.env.TRUST_PROXY || "loopback");

  const metrics = createMetrics();
  app.use(metrics.middleware);

  const store = new PgUserStore(pool, { adminEmails: config.adminEmails, agentEmails: agentEmailsFromEnv() });
  const sessionStore = createSessionStore(pool);
  const auth = createAuth(config, { store, sessionStore });
  app.use(auth.router);

  const password = createPasswordAuth(pool, { metrics });
  app.use(password.router);

  // Mounted before the tickets router: its POST /boss/incidents/:groupId callback needs the
  // raw request body to verify BOSS's HMAC signature, and the tickets router's own
  // `router.use(express.json())` (unscoped, so it runs for every request that reaches it)
  // would otherwise consume the body stream first and leave nothing to verify against.
  const incidents = createIncidentsRouter({ pool, requireRole: auth.requireRole, metrics });
  app.use(incidents.router);

  const tickets = createTicketsRouter({
    pool,
    requireUser: auth.requireUser,
    requireRole: auth.requireRole,
    suggest: suggestForTicket,
    metrics,
  });
  app.use(tickets.router);

  const avatar = createAvatarRouter({ pool, metrics });
  app.use(avatar.router);

  const flags = createFlagsRouter({ metrics });
  app.use(flags.router);

  app.get("/metrics", (req, res) => metrics.handler(req, res));

  app.use(express.static(WEB_DIST));
  app.use((req, res, next) => {
    if (req.method !== "GET") return next();
    if (req.path.startsWith("/api") || req.path.startsWith("/auth") || req.path.startsWith("/avatar") || req.path.startsWith("/boss") || req.path === "/metrics") {
      return next();
    }
    res.sendFile(path.join(WEB_DIST, "index.html"), (err) => (err ? next() : undefined));
  });

  // eslint-disable-next-line no-unused-vars
  app.use((err, req, res, next) => {
    console.error(err);
    res.status(500).json({ error: "internal_error" });
  });

  app.locals.auth = auth;
  return app;
}
