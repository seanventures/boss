// Held by the benchmark, never shown to the agent: run on the pull request's head.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { slug } from "../src/slug.js";

test("slug lowercases, joins words with single hyphens and trims them", () => {
  assert.equal(slug("Hello, World"), "hello-world");
  assert.equal(slug("  Many   spaces  here "), "many-spaces-here");
  assert.equal(slug("--Already-Slugged--"), "already-slugged");
  assert.equal(slug(""), "");
  assert.equal(slug(undefined), "");
});

function freePort() {
  return new Promise((resolve) => {
    const s = createServer().listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

test("the server answers GET /?q= with the slug as JSON on $PORT", async () => {
  const port = await freePort();
  const child = spawn(process.execPath, ["src/server.js"], {
    env: { ...process.env, PORT: String(port) },
    stdio: "ignore",
  });
  try {
    let body = null;
    for (let i = 0; i < 50 && body === null; i++) {
      try {
        const res = await fetch(`http://127.0.0.1:${port}/?q=${encodeURIComponent("Ship It Now")}`);
        assert.match(res.headers.get("content-type") || "", /json/);
        body = await res.json();
      } catch {
        await new Promise((r) => setTimeout(r, 100));
      }
    }
    assert.deepEqual(body, { slug: "ship-it-now" });
  } finally {
    child.kill();
  }
});
