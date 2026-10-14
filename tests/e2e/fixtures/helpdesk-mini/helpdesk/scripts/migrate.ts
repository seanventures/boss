import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import pg from "pg";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const migrationsDir = path.join(__dirname, "..", "migrations");

async function main() {
  const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });
  try {
    await pool.query(
      "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMP NOT NULL DEFAULT now())",
    );
    const applied = new Set(
      (await pool.query("SELECT name FROM schema_migrations")).rows.map((r: { name: string }) => r.name),
    );
    for (const file of readdirSync(migrationsDir).filter((f) => f.endsWith(".sql")).sort()) {
      if (applied.has(file)) continue;
      await pool.query("BEGIN");
      try {
        await pool.query(readFileSync(path.join(migrationsDir, file), "utf8"));
        await pool.query("INSERT INTO schema_migrations (name) VALUES ($1)", [file]);
        await pool.query("COMMIT");
        console.log(`applied ${file}`);
      } catch (err) {
        await pool.query("ROLLBACK");
        throw err;
      }
    }
  } finally {
    await pool.end();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
