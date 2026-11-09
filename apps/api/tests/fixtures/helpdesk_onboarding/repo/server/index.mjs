import { loadConfig } from "./auth/config.mjs";
import { createPool, migrate } from "./db.mjs";
import { createApp } from "./app.mjs";
import { seedDemoUsers } from "./seedDemoUsers.mjs";
import { seedDemoTickets } from "./seedDemoTickets.mjs";

const config = loadConfig();
const pool = createPool(process.env.DATABASE_URL);
await migrate(pool);
await seedDemoUsers(pool);
await seedDemoTickets(pool);

const app = createApp({ config, pool });
const port = Number(process.env.PORT || 3000);
const host = process.env.HOST || "0.0.0.0";

app.listen(port, host, () => {
  console.log(`HelpDesk AI listening on ${host}:${port}`);
});
