import { randomBytes } from "node:crypto";

// The tests start the app in-process. A random signing secret for this run only, never a value
// written into the code (the connector refuses to start without one).
if (!process.env.SESSION_SECRET) process.env.SESSION_SECRET = randomBytes(32).toString("hex");
