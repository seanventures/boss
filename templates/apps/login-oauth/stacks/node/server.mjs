// A runnable example: an Express app with login-oauth mounted. In an existing app, mount
// `auth.router` on the app's own Express instance and protect routes with auth.requireUser /
// auth.requireRole("admin").
import express from "express";
import { pathToFileURL } from "node:url";

import { loadConfig } from "./auth/config.mjs";
import { createAuth } from "./auth/routes.mjs";

export function createApp(config = loadConfig(), options = {}) {
  const app = express();
  app.disable("x-powered-by");
  // X-Forwarded-Proto is believed only from a proxy on this machine (the preview/deploy proxy),
  // so Secure cookies work behind it and a client cannot spoof https.
  app.set("trust proxy", process.env.TRUST_PROXY || "loopback");
  const auth = createAuth(config, options);
  app.use(auth.router);
  app.get("/", (req, res) => {
    const user = auth.currentUser(req);
    res.type("text/plain").send(user ? `Signed in as ${user.email || user.name}` : "Signed out");
  });
  app.locals.auth = auth;
  return app;
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  const port = Number(process.env.PORT || 3000);
  createApp().listen(port, () => console.log(`listening on :${port}`));
}
