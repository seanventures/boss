import { randomUUID } from "node:crypto";
import bcrypt from "bcryptjs";

export const DEMO_USERS = [
  { email: "customer@demo.test", role: "customer", name: "Demo Customer" },
  { email: "agent@demo.test", role: "agent", name: "Demo Agent" },
  { email: "admin@demo.test", role: "admin", name: "Demo Admin" },
];

// ALLOW_DEMO_SEED unlocks a single shared password across every demo identity — acceptable only
// on the staging deploy that needs it for reviewer sign-in, never elsewhere. APP_ENV defaults to
// "staging" because this is exactly what helpdesk-ai/docker-compose.yml ships as; an operator
// who deploys this image anywhere else must set APP_ENV explicitly to something other than
// "staging" to get this guard.
export function assertDemoSeedEnvironmentAllowed(env = process.env) {
  if (env.ALLOW_DEMO_SEED === "true" && (env.APP_ENV || "staging") !== "staging") {
    throw new Error("ALLOW_DEMO_SEED=true is only permitted when APP_ENV=staging");
  }
}

// Seeds/refreshes fixed demo accounts so reviewers can log in without a signup flow. Only runs
// when explicitly opted into via env; the password is hashed before storage and never logged.
export async function seedDemoUsers(pool, env = process.env) {
  assertDemoSeedEnvironmentAllowed(env);
  if (env.ALLOW_DEMO_SEED !== "true" || !env.DEMO_PASSWORD) return false;
  const passwordHash = await bcrypt.hash(env.DEMO_PASSWORD, 10);
  for (const user of DEMO_USERS) {
    await pool.query(
      `INSERT INTO users (id, email, email_verified, name, role, password_hash)
       VALUES ($1, $2, TRUE, $3, $4, $5)
       ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role, password_hash = EXCLUDED.password_hash, name = EXCLUDED.name`,
      [randomUUID(), user.email, user.name, user.role, passwordHash],
    );
  }
  console.log(`Demo users seeded: ${DEMO_USERS.map((u) => u.email).join(", ")}`);
  return true;
}
