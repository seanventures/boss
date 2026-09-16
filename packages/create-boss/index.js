#!/usr/bin/env node
/**
 * Stand up BOSS locally in one command.
 *
 * This is LOCAL_SETUP.md executed rather than read. Every step it runs is a step that document
 * describes, in the same order, so a reader who wants to understand what happened — or to do it
 * by hand on a machine this cannot reach — has one place to look.
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

const { spawn, spawnSync } = require("child_process");
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
  const lines = Object.entries(pairs)
    // NEVER overwrite. Re-running must not rotate a key: every secret already in the store was
    // encrypted with the old one and would become unreadable.
    .filter(([key]) => existing[key] === undefined)
    .map(([key, value]) => `${key}=${value}`);
  if (lines.length) fs.appendFileSync(file, `${lines.join("\n")}\n`, { mode: 0o600 });
  return lines.length;
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
      stdio: ["ignore", "inherit", "inherit"],
    });
    child.on("close", resolve);
  });
}

async function main() {
  const root = repoRoot();
  const envFile = path.resolve(root, process.argv[2] || ".env");

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
  for (const [name, preferred, what] of PORTS) {
    const current = readEnv(envFile)[name];
    const port = current ? Number(current) : await freePort(preferred, claimed);
    claimed.add(port);
    chosen[name] = port;
    if (!current && port !== preferred) note(`${preferred} is taken; using ${port} for the ${what}`);
  }
  if (new Set(Object.values(chosen)).size !== Object.keys(chosen).length) {
    die(`Two services were given the same port: ${JSON.stringify(chosen)}`,
        `Remove the *_PORT lines from ${envFile} and run again.`);
  }
  appendEnv(envFile, chosen);
  appendEnv(envFile, { INFISICAL_SITE_URL: `http://127.0.0.1:${chosen.INFISICAL_PORT}` });
  ok("ports chosen");

  step(2, "Building and starting containers (first run takes a few minutes)");
  const up = await composeStream(root, envFile, [...PROFILES, "up", "-d", "--build"]);
  if (up !== 0) die("docker compose could not start the stack.");
  await waitFor(`http://127.0.0.1:${chosen.BOSS_API_PORT}/health`, 240, "The API");
  ok("all containers up");

  step(3, "Creating the database contents");
  const seed = compose(root, envFile, ["exec", "-T", "api", "python", "-m", "boss_engine", "bootstrap"], {
    capture: true,
  });
  if (seed.status !== 0) die("Could not seed the database.", (seed.stderr || "").trim().slice(-300));
  ok("schema applied and defaults seeded");

  step(4, "Provisioning the secret store");
  const already = readEnv(envFile).BOSS_INFISICAL_TOKEN;
  if (already) {
    ok("already provisioned, left alone");
  } else {
    await waitFor(`http://127.0.0.1:${chosen.INFISICAL_PORT}/api/status`, 180, "The secret store");
    const boot = run("sh", ["scripts/install/bootstrap_infisical.sh", envFile], {
      cwd: root,
      capture: true,
    });
    if (boot.status !== 0) {
      die("Could not provision the secret store.", (boot.stderr || "").trim().slice(-400));
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

BOSS ships with no model — connect one from the console (Setup → Model), or see
docs/operations/MODEL_AND_SUBSCRIPTION_SETUP.md.
`);
}

main().catch((error) => die(error && error.message ? error.message : String(error)));
