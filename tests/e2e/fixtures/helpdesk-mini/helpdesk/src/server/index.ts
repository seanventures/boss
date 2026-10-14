import { createApp } from "./app";
import { getHost, getPort } from "./config";

// HelpDesk AI's boot (crun_0d82ab62): its database and its demo seed's password are required at
// start. BOSS supplies both to every run it starts: the run's own DATABASE_URL (build services)
// and a generated DEMO_PASSWORD (templates.run_env "demo").
for (const name of ["DATABASE_URL", "DEMO_PASSWORD"]) {
  if (!process.env[name]) {
    console.error(JSON.stringify({ event: "startup_failed",
      message: `${name} must be set (the seed script and demo logins refuse to run without it)` }));
    process.exit(1);
  }
}

createApp()
  .then((app) => {
    const host = getHost();
    const port = getPort();
    app.listen(port, host, () => {
      console.log(`HelpDesk mini listening on ${host}:${port}`);
    });
  })
  .catch((err) => {
    console.error("failed to start HelpDesk mini:", err instanceof Error ? err.message : err);
    process.exit(1);
  });
