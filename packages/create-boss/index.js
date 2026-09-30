#!/usr/bin/env node
/**
 * Stand up BOSS locally in one command.
 *
 * This is Part 1 of SETUP.md executed rather than read. Every step it runs is a step that
 * document describes, in the same order, so a reader who wants to understand what happened — or
 * to do it by hand on a machine this cannot reach — has one place to look.
 *
 * Two things it does that the document cannot:
 *
 *   * it picks free ports. The defaults collide on any machine that already runs Postgres, and
 *     a developer machine usually does. "port is already allocated" arrives from Docker with no
 *     hint of which of the four ports it means.
 *   * it is safe to re-run. It never regenerates a key that exists, because rotating
 *     INFISICAL_ENCRYPTION_KEY strands every secret encrypted under the old one.
 *
 * It builds from this repository, so it needs a clone; BOSS publishes no images yet. When it
 * does, this becomes `npx create-boss` from nothing at all.
 */
"use strict";

const { execFileSync, spawn, spawnSync } = require("child_process");
const fs = require("fs");
const net = require("net");
const path = require("path");

const COMPOSE = "scripts/install/community-compose.yml";
const PROFILES = ["--profile", "secrets", "--profile", "web"];
const PORTS = [
  ["BOSS_POSTGRES_PORT", 5432, "database"],
  ["INFISICAL_PORT", 8080, "secret store"],
  ["BOSS_API_PORT", 8000, "api"],
  ["BOSS_WEB_PORT", 3000, "console"],
];
const BUILD_STAMPS = new Map();

const step = (n, what) => console.log(`\n[${n}/6] ${what}`);
const ok = (what) => console.log(`      ✓ ${what}`);
const note = (what) => console.log(`      ${what}`);

function die(message, hint) {
  console.error(`\n✗ ${message}`);
  if (hint) console.error(`  ${hint}`);
  process.exit(1);
}

function run(cmd, args, { capture = false, env = process.env, cwd } = {}) {
  const result = spawnSync(cmd, args, {
    cwd,
    env,
    encoding: "utf8",
    stdio: capture ? ["ignore", "pipe", "pipe"] : ["ignore", "inherit", "inherit"],
  });
  return result;
}

/** Docker's own failure text is long and buries the useful line; surface only that. */
function requireDocker() {
  const version = run("docker", ["version", "--format", "{{.Server.Version}}"], { capture: true });
  if (version.error || version.status !== 0) {
    die("Docker is not available.", "Install Docker Desktop and make sure it is running.");
  }
  return version.stdout.trim();
}

function repoRoot() {
  // Works from the package directory, from the repo root, and from an npm global install.
  let dir = process.cwd();
  for (let i = 0; i < 6; i += 1) {
    if (fs.existsSync(path.join(dir, COMPOSE))) return dir;
    dir = path.dirname(dir);
  }
  const packaged = path.resolve(__dirname, "..", "..");
  if (fs.existsSync(path.join(packaged, COMPOSE))) return packaged;
  die(
    "Could not find a BOSS checkout.",
    "Run this from a clone of the repository — it builds the images from source."
  );
  return "";
}

/**
 * Is this port usable?
 *
 * Two tests, because either alone lies. Binding 127.0.0.1 succeeds on macOS even when another
 * process holds the 0.0.0.0 wildcard — SO_REUSEADDR permits the narrower bind — so the obvious
 * check reported 5432 free on a machine where Docker was already publishing it, and setup died
 * with "port is already allocated" a few minutes later. Binding the wildcard catches that.
 * Connecting catches the reverse: something bound to 127.0.0.1 only.
 */
function portIsFree(port) {
  const canBind = (host) =>
    new Promise((resolve) => {
      const server = net.createServer();
      server.once("error", () => resolve(false));
      server.once("listening", () => server.close(() => resolve(true)));
      server.listen(port, host);
    });
  const answers = () =>
    new Promise((resolve) => {
      const socket = net.connect({ host: "127.0.0.1", port });
      socket.setTimeout(1500);
      socket.once("connect", () => { socket.destroy(); resolve(true); });
      socket.once("error", () => resolve(false));
      socket.once("timeout", () => { socket.destroy(); resolve(false); });
    });
  return (async () => (await canBind("0.0.0.0")) && !(await answers()))();
}

/**
 * A port nothing is using AND nothing else in this run has claimed.
 *
 * `taken` is not optional bookkeeping. Ports are chosen before any container starts, so a port
 * handed to the database is still unused when the secret store asks — and both were assigned
 * 55700, which Docker rejected as "already allocated" after a full build.
 */
/**
 * Ports this stack is already publishing.
 *
 * "In use" by our OWN containers is not a conflict — it is the normal state of a second run.
 * Without this, re-running moved every port: each configured one probed as occupied, was
 * declared unusable and replaced, so the console and API landed somewhere new every time and
 * any address the operator had saved stopped working.
 */
function ourPublishedPorts() {
  const ps = spawnSync(
    "docker",
    ["ps", "--filter", "label=com.docker.compose.project=boss-community", "--format", "{{.Ports}}"],
    { encoding: "utf8" }
  );
  const ports = new Set();
  for (const match of (ps.stdout || "").matchAll(/:(\d+)->/g)) ports.add(Number(match[1]));
  return ports;
}

function freePort(preferred, taken) {
  return (async () => {
    if (!taken.has(preferred) && (await portIsFree(preferred))) return preferred;
    for (let port = 55700; port < 55800; port += 1) {
      if (!taken.has(port) && (await portIsFree(port))) return port;
    }
    die(`No free port for ${preferred}.`);
    return preferred;
  })();
}

function readEnv(file) {
  if (!fs.existsSync(file)) return {};
  const out = {};
  for (const line of fs.readFileSync(file, "utf8").split("\n")) {
    const at = line.indexOf("=");
    if (at > 0 && !line.startsWith("#")) out[line.slice(0, at)] = line.slice(at + 1);
  }
  return out;
}

function appendEnv(file, pairs) {
  const existing = readEnv(file);
  const missing = Object.entries(pairs)
    // NEVER overwrite a real value. Re-running must not rotate a key: every secret already in
    // the store was encrypted with the old one and would become unreadable.
    //
    // But BLANK IS MISSING, not set. This tested `=== undefined`, so a placeholder line like
    // `BOSS_API_KEY=` — which repositories carry as a template — counted as configured. The
    // installer generated nothing, every service came up with an empty key, and the failure
    // surfaced much later as "no BOSS_API_KEY to authenticate to BOSS with" from an unrelated
    // script.
    .filter(([key]) => !(existing[key] || "").trim());
  const lines = missing.map(([key, value]) => `${key}=${value}`);
  if (!lines.length) return 0;
  // setEnv rather than append, so a blank line is REPLACED instead of duplicated.
  setEnv(file, Object.fromEntries(missing));
  return lines.length;
}

/**
 * Set keys, replacing any line that already sets them.
 *
 * Unlike `appendEnv`, this overwrites — which is right for a port and wrong for a key. A port
 * that cannot be bound is worthless, so keeping it helps nobody; an encryption key that is
 * replaced strands every secret encrypted under the old one.
 */
function setEnv(file, pairs) {
  const lines = fs.existsSync(file) ? fs.readFileSync(file, "utf8").split("\n") : [];
  const remaining = new Map(Object.entries(pairs));
  const out = [];
  for (const line of lines) {
    const at = line.indexOf("=");
    const key = at > 0 ? line.slice(0, at) : "";
    if (key && remaining.has(key)) {
      out.push(`${key}=${remaining.get(key)}`);
      remaining.delete(key);
      continue;
    }
    out.push(line);
  }
  while (out.length && out[out.length - 1] === "") out.pop();
  for (const [key, value] of remaining) out.push(`${key}=${value}`);
  fs.writeFileSync(file, `${out.join("\n")}\n`, { mode: 0o600 });
}

/** Remove these keys entirely, so a later step treats them as absent rather than empty. */
function dropEnv(file, keys) {
  if (!fs.existsSync(file)) return;
  const drop = new Set(keys);
  const kept = fs
    .readFileSync(file, "utf8")
    .split("\n")
    .filter((line) => {
      const at = line.indexOf("=");
      return !(at > 0 && drop.has(line.slice(0, at)));
    });
  while (kept.length && kept[kept.length - 1] === "") kept.pop();
  fs.writeFileSync(file, `${kept.join("\n")}\n`, { mode: 0o600 });
}

/**
 * Wait for the secret store, checking WHY it is not answering as we go.
 *
 * The first version polled in silence for three minutes and only then looked at the logs. A
 * container that is crash-looping cannot become healthy, so every second of that wait was
 * spent on a foregone conclusion — and from the outside it is indistinguishable from a hang,
 * which is precisely how it was reported. Diagnosing early turns it into about fifteen
 * seconds, and saying so while waiting turns the rest into visible progress.
 *
 * Returns "ready", "key-mismatch", or "timeout".
 */
async function waitForSecretStore(root, envFile, port, seconds = 180) {
  const url = `http://127.0.0.1:${port}/api/status`;
  const deadline = Date.now() + seconds * 1000;
  let elapsed = 0;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(4000) });
      if (response.ok) return "ready";
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 2000));
    elapsed += 2;
    // Give it a moment to fail on its own terms before reading anything into the logs.
    if (elapsed % 10 === 0) {
      if (diagnoseSecretStore(root, envFile) === "key-mismatch") return "key-mismatch";
      note(`still waiting for the secret store (${elapsed}s)`);
    }
  }
  return "timeout";
}

async function waitFor(url, seconds, what) {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(4000) });
      if (response.ok) return true;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  die(`${what} did not come up within ${seconds}s.`, `Check: docker compose -f ${COMPOSE} ps`);
  return false;
}

function compose(root, envFile, args, opts = {}) {
  return run("docker", ["compose", "-f", COMPOSE, "--env-file", envFile, ...args], {
    cwd: root,
    ...opts,
  });
}

function composeStream(root, envFile, args) {
  return new Promise((resolve) => {
    const child = spawn("docker", ["compose", "-f", COMPOSE, "--env-file", envFile, ...args], {
      cwd: root,
      // STAMP THE BUILD WITH THE COMMIT IT CAME FROM. Without it the running image cannot say
      // which source it is, so "is the fix I just read actually deployed?" has no answer —
      // the container carries the source but not the repository.
      env: { ...process.env, ...buildStamp(root) },
      stdio: ["ignore", "inherit", "inherit"],
    });
    child.on("close", resolve);
  });
}

function buildStamp(root) {
  if (BUILD_STAMPS.has(root)) return BUILD_STAMPS.get(root);
  let sha = "unknown";
  try {
    sha = execFileSync("git", ["-C", root, "rev-parse", "--short", "HEAD"],
                       { encoding: "utf8" }).trim() || "unknown";
    const dirty = execFileSync("git", ["-C", root, "status", "--porcelain"],
                               { encoding: "utf8" }).trim();
    if (dirty) sha += "-dirty";          // a developer's working tree is not a commit
  } catch { /* not a clone, or no git — "unknown" is the honest answer */ }
  const stamp = { BOSS_SOURCE_SHA: sha, BOSS_BUILT_AT: new Date().toISOString() };
  BUILD_STAMPS.set(root, stamp);
  return stamp;
}

/** Build all images first, then seed the API before any worker tick can run. */
async function startSeededStack(root, envFile, apiPort, {
  stream = composeStream,
  command = compose,
  wait = waitFor,
  fail = die,
} = {}) {
  step(2, "Building containers and starting the database and API (first run takes a few minutes)");
  const built = await stream(root, envFile, [...PROFILES, "build"]);
  if (built !== 0) fail("docker compose could not build the stack.");
  const core = await stream(root, envFile, [...PROFILES, "up", "-d", "db", "api"]);
  if (core !== 0) fail("docker compose could not start the database and API.");
  await wait(`http://127.0.0.1:${apiPort}/health`, 240, "The API");
  ok("database and API ready");

  step(3, "Creating the database contents");
  const seed = command(root, envFile, ["exec", "-T", "api", "python", "-m", "boss_engine", "bootstrap"], {
    capture: true,
  });
  if (seed.status !== 0) fail("Could not seed the database.", (seed.stderr || "").trim().slice(-300));
  ok("schema applied and defaults seeded");

  step(4, "Starting the worker, console, and secret store");
  const all = await stream(root, envFile, [...PROFILES, "up", "-d"]);
  if (all !== 0) fail("docker compose could not start the stack.");
  ok("all containers up");
}

/**
 * Can we actually use the secret store these settings describe?
 *
 * Asked of the store, not of the settings file. A token in `.env` proves somebody provisioned
 * something once; it does not prove this instance still honours it. Reinstalling over a volume
 * from an earlier install leaves a store bootstrapped under a DIFFERENT encryption key, where
 * the account exists and the credential is refused.
 */
async function infisicalUsable(port, token, projectId) {
  if (!token || !projectId) return false;
  const query = `workspaceId=${encodeURIComponent(projectId)}&environment=prod&secretPath=%2F`;
  try {
    const response = await fetch(`http://127.0.0.1:${port}/api/v3/secrets/raw?${query}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(10000),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/** Drop and recreate Infisical's database, so it re-initialises under the current key. */
async function resetSecretStore(root, envFile, port) {
  note("resetting the secret store (everything it holds is discarded)");
  const sql = compose(root, envFile, [
    "exec", "-T", "db", "psql", "-U", "boss", "-d", "postgres",
    "-c", "DROP DATABASE IF EXISTS infisical WITH (FORCE);",
    "-c", "CREATE DATABASE infisical OWNER boss;",
  ], { capture: true });
  if (sql.status !== 0) {
    die("Could not reset the secret store.", (sql.stderr || "").trim().slice(-300));
  }
  await composeStream(root, envFile, [...PROFILES, "restart", "infisical"]);
  await waitFor(`http://127.0.0.1:${port}/api/status`, 180, "The secret store");
  // The stale values name a store that no longer exists. Dropping the LINES rather than
  // blanking them matters: bootstrap_infisical.sh skips a key the file already mentions, so a
  // blank BOSS_INFISICAL_TOKEN= would survive provisioning and leave the install unusable.
  dropEnv(envFile, ["BOSS_INFISICAL_URL", "BOSS_INFISICAL_TOKEN", "BOSS_INFISICAL_PROJECT_ID",
                    "INFISICAL_ADMIN_EMAIL", "INFISICAL_ADMIN_PASSWORD"]);
}

/**
 * Why will the secret store not start?
 *
 * One cause is common enough to name: installing over a volume from an earlier install leaves
 * Infisical's data encrypted under that install's INFISICAL_ENCRYPTION_KEY, and it dies trying
 * to decrypt its own root key. The container restart-loops and every health probe times out,
 * so without this the installer waits three minutes and reports nothing useful.
 */
function diagnoseSecretStore(root, envFile) {
  const logs = compose(root, envFile, ["logs", "--tail", "40", "infisical"], { capture: true });
  const text = `${logs.stdout || ""}${logs.stderr || ""}`;
  if (/decryptRootKey|unable to authenticate data/i.test(text)) return "key-mismatch";
  return "unknown";
}

async function main() {
  const args = process.argv.slice(2);
  const resetRequested = args.includes("--reset-secret-store");
  const root = repoRoot();
  const envFile = path.resolve(root, args.find((a) => !a.startsWith("--")) || ".env");

  console.log("Setting up BOSS locally.");
  note(`repository ${root}`);
  note(`settings   ${envFile}`);

  step(1, "Settings");
  const dockerVersion = requireDocker();
  ok(`Docker ${dockerVersion}`);

  const crypto = require("crypto");
  const added = appendEnv(envFile, {
    BOSS_API_KEY: crypto.randomBytes(16).toString("hex"),
    INFISICAL_ENCRYPTION_KEY: crypto.randomBytes(16).toString("hex"),
    INFISICAL_AUTH_SECRET: crypto.randomBytes(32).toString("base64"),
  });
  ok(added ? `wrote ${added} new setting(s)` : "settings already present, left alone");

  const chosen = {};
  const claimed = new Set();
  const ours = ourPublishedPorts();
  for (const [name, preferred, what] of PORTS) {
    const current = Number(readEnv(envFile)[name]) || 0;
    if (current && ours.has(current) && !claimed.has(current)) {
      claimed.add(current);
      chosen[name] = current;
      continue;   // already ours; moving it would break every saved address
    }
    // A CONFIGURED PORT IS A PREFERENCE, NOT A PROMISE. Trusting one because it was already in
    // the file meant a stale BOSS_POSTGRES_PORT=5432 from somebody's dev setup was used
    // unchecked, and setup died on "port is already allocated" after a full rebuild. Nothing is
    // served by keeping a port that cannot be bound, so it is re-picked and written back.
    const usable = current && !claimed.has(current) && (await portIsFree(current));
    const port = usable ? current : await freePort(current || preferred, claimed);
    claimed.add(port);
    chosen[name] = port;
    if (current && port !== current) {
      note(`${name}=${current} is in use; switching to ${port}`);
    } else if (!current && port !== preferred) {
      note(`${preferred} is taken; using ${port} for the ${what}`);
    }
  }
  if (new Set(Object.values(chosen)).size !== Object.keys(chosen).length) {
    die(`Two services were given the same port: ${JSON.stringify(chosen)}`,
        `Remove the *_PORT lines from ${envFile} and run again.`);
  }
  setEnv(envFile, { ...chosen, INFISICAL_SITE_URL: `http://127.0.0.1:${chosen.INFISICAL_PORT}` });
  ok("ports chosen");

  await startSeededStack(root, envFile, chosen.BOSS_API_PORT);

  // The full stack is running only after the project and its default members exist.
  // The secret store comes up in that second start and can now be provisioned.
  const port = chosen.INFISICAL_PORT;

  // DIAGNOSE WHILE WAITING, not after. A store that will never come up looks exactly like one
  // that is still booting, and waiting out the full timeout first turns a known failure into
  // three silent minutes that read as a hang.
  const state = await waitForSecretStore(root, envFile, port);
  if (state !== "ready") {
    if (state === "timeout") {
      die("The secret store did not come up.", `Look at: docker compose -f ${COMPOSE} logs infisical`);
    }
    note("the secret store cannot read its own data");
    note("it was set up under a different INFISICAL_ENCRYPTION_KEY — usually a volume left by");
    note("an earlier install. Nothing in it is readable without that original key.");
    if (!resetRequested) {
      die(
        "Refusing to discard a secret store without being told to.",
        "Re-run with --reset-secret-store to wipe and rebuild it, or restore the original " +
          `INFISICAL_ENCRYPTION_KEY into ${envFile}.`
      );
    }
    await resetSecretStore(root, envFile, port);
  }

  const settingsNow = readEnv(envFile);
  const usable = await infisicalUsable(port, settingsNow.BOSS_INFISICAL_TOKEN,
                                       settingsNow.BOSS_INFISICAL_PROJECT_ID);
  if (usable) {
    ok("already provisioned, left alone");
  } else {
    if (settingsNow.BOSS_INFISICAL_TOKEN) {
      // The file says provisioned and the store disagrees. Believe the store.
      note("the stored credentials no longer work; provisioning again");
      dropEnv(envFile, ["BOSS_INFISICAL_URL", "BOSS_INFISICAL_TOKEN", "BOSS_INFISICAL_PROJECT_ID"]);
    }
    const boot = run("sh", ["scripts/install/bootstrap_infisical.sh", envFile], {
      cwd: root,
      capture: true,
    });
    const provisioned = Boolean(readEnv(envFile).BOSS_INFISICAL_TOKEN);
    if (boot.status !== 0 || !provisioned) {
      const detail = `${boot.stdout || ""}${boot.stderr || ""}`;
      if (/already been bootstrapped|no token/i.test(detail) && !resetRequested) {
        die(
          "The secret store is already set up, but this install has no credential for it.",
          "Infisical issues that token once. Re-run with --reset-secret-store to rebuild the " +
            "store, or put the original BOSS_INFISICAL_TOKEN back in your settings file."
        );
      }
      die("Could not provision the secret store.", detail.trim().slice(-400));
    }
    ok("admin, organization, project and machine identity created");
  }

  step(5, "Restarting BOSS so it reads the new settings");
  const restart = await composeStream(root, envFile, [...PROFILES, "up", "-d", "api", "worker"]);
  if (restart !== 0) die("Could not restart BOSS.");
  await waitFor(`http://127.0.0.1:${chosen.BOSS_API_PORT}/health`, 180, "The API");

  step(6, "Checking where secrets will be kept");
  const backend = compose(
    root,
    envFile,
    [
      "exec", "-T", "api", "python", "-c",
      "from boss_engine.config import Settings; from boss_engine.services.secret_backends import default_secret_backend; print(default_secret_backend(Settings()))",
    ],
    { capture: true }
  );
  const where = (backend.stdout || "").trim().split("\n").pop();
  if (where === "infisical") {
    ok("secrets go to Infisical");
  } else {
    // Not fatal: BOSS works, and it is better to say plainly where credentials will land than
    // to fail a setup that otherwise succeeded.
    note(`⚠ secrets would go to "${where}", not Infisical. Check BOSS_INFISICAL_* in ${envFile}.`);
  }

  const settings = readEnv(envFile);
  console.log(`
BOSS is running.

  Console       http://127.0.0.1:${chosen.BOSS_WEB_PORT}/?org=org_boss
  API           http://127.0.0.1:${chosen.BOSS_API_PORT}
  Secret store  http://127.0.0.1:${chosen.INFISICAL_PORT}   (${settings.INFISICAL_ADMIN_EMAIL || "admin@boss.local"})

Credentials are in ${envFile}. Back it up: INFISICAL_ENCRYPTION_KEY is the only way to read
what the secret store holds.

BOSS ships with no model — connect one from the console (Setup → Model), or see Part 2 of
SETUP.md. This install runs in containers, so a subscription you are signed
into on this machine still has to be sent across: that is Path C, not Path B.
`);
}

if (require.main === module) {
  main().catch((error) => die(error && error.message ? error.message : String(error)));
}

module.exports = { startSeededStack };
