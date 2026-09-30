"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { startSeededStack } = require("../index.js");

const fullStack = ["--profile", "secrets", "--profile", "web", "up", "-d"];

function harness(seedStatus = 0) {
  const events = [];
  const dependencies = {
    stream: async (_root, _envFile, args) => {
      events.push(["compose", args]);
      return 0;
    },
    wait: async (url) => {
      events.push(["healthy", url]);
    },
    command: (_root, _envFile, args) => {
      events.push(["exec", args]);
      return { status: seedStatus, stderr: "seed failed" };
    },
    fail: (message) => { throw new Error(message); },
  };
  return { events, dependencies };
}

test("fresh install seeds the project before starting worker or console", async () => {
  const { events, dependencies } = harness();
  await startSeededStack("/boss", ".env", 8000, dependencies);

  assert.deepEqual(events, [
    ["compose", ["--profile", "secrets", "--profile", "web", "build"]],
    ["compose", ["--profile", "secrets", "--profile", "web", "up", "-d", "db", "api"]],
    ["healthy", "http://127.0.0.1:8000/health"],
    ["exec", ["exec", "-T", "api", "python", "-m", "boss_engine", "bootstrap"]],
    ["compose", fullStack],
  ]);
});

test("seed failure does not start the worker", async () => {
  const { events, dependencies } = harness(1);
  await assert.rejects(
    startSeededStack("/boss", ".env", 8000, dependencies),
    /Could not seed the database/,
  );
  assert.equal(events.some(([kind, args]) => kind === "compose" &&
    JSON.stringify(args) === JSON.stringify(fullStack)), false);
});
