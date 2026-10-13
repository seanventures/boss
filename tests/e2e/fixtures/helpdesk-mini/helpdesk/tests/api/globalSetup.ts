import { execSync } from "node:child_process";

// Once before the suite: migrate the real Postgres the harness provides (DATABASE_URL).
export default async function setup() {
  execSync("npm run migrate", { stdio: "inherit", env: process.env });
}
