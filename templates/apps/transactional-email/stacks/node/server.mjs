// A runnable example: an Express app with the transactional-email connector mounted. In an
// existing app, mount `email.router` on the app's own Express instance and wire the hooks to the
// app's own users (findUserByEmail, setPassword), its sign-in and roles (canInvite, which also
// checks the app's CSRF token) and its membership (onInviteAccepted). Call email.notify(...) from
// the app's own code where something happens that a person should hear about.
import express from "express";
import { pathToFileURL } from "node:url";

import { loadEmailConfig } from "./email/config.mjs";
import { createEmail } from "./email/routes.mjs";

export function createApp(config = loadEmailConfig(), options = {}) {
  const app = express();
  app.disable("x-powered-by");
  app.set("trust proxy", process.env.TRUST_PROXY || "loopback");
  const email = createEmail(config, options);
  app.use(email.router);
  app.get("/", (req, res) => {
    res.type("text/plain").send(`Email goes through: ${email.transport.name}`);
  });
  app.locals.email = email;
  return app;
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  const port = Number(process.env.PORT || 3000);
  createApp().listen(port, () => console.log(`listening on :${port}`));
}
