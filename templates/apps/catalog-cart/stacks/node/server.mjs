// A runnable example: an Express app with catalog-cart mounted. In an existing app, mount
// `catalog.router` on the app's own Express instance (after login-oauth's router when both are
// applied, so the session's user is known and the anonymous cart merges into theirs).
import express from "express";
import { pathToFileURL } from "node:url";

import { createCatalog, loadCatalogConfig } from "./catalog/routes.mjs";

export function createApp(config = loadCatalogConfig(), options = {}) {
  const app = express();
  app.disable("x-powered-by");
  app.set("trust proxy", process.env.TRUST_PROXY || "loopback");
  const catalog = createCatalog(config, options);
  app.use(catalog.router);
  app.locals.catalog = catalog;
  return app;
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  const port = Number(process.env.PORT || 3000);
  createApp().listen(port, () => console.log(`listening on :${port}`));
}
