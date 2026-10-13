import { createApp } from "./app";
import { getHost, getPort } from "./config";

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
