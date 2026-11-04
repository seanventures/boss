// The `hello-world` template's one test, against the HTTP page (GET /), not internals.
import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { handler } = require("../../server.js");

test("GET / says hello and shows the time", async () => {
  const server = http.createServer(handler);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  try {
    const res = await fetch(`http://127.0.0.1:${port}/`);
    const body = await res.text();
    assert.equal(res.status, 200);
    assert.match(body, /<h1>Hello, .+!<\/h1>/);
    assert.match(body, /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
  } finally {
    server.close();
  }
});
