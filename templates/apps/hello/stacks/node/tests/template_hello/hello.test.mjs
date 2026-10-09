// The `hello` template's own tests, against the HTTP route (GET /hello), not internals.
import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { handleHello } = require("../../hello.js");

async function call(method) {
  const server = http.createServer(handleHello);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  try {
    const res = await fetch(`http://127.0.0.1:${port}/hello`, { method });
    return { status: res.status, body: await res.text() };
  } finally {
    server.close();
  }
}

test("GET /hello answers the greeting as JSON", async () => {
  const { status, body } = await call("GET");
  assert.equal(status, 200);
  assert.deepEqual(JSON.parse(body), { message: process.env.HELLO_GREETING || "hello from BOSS" });
});

test("POST /hello is refused with 405", async () => {
  const { status } = await call("POST");
  assert.equal(status, 405);
});
