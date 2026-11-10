import { after, before, beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { handle } from "../src/app.js";
import { resetStore } from "../src/store.js";

let server;
let base;

before(async () => {
  server = createServer(handle);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});

after(() => server.close());
beforeEach(() => resetStore());

async function call(method, path, body) {
  const res = await fetch(base + path, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: res.status, body: await res.json() };
}

test("health answers ok", async () => {
  assert.deepEqual(await call("GET", "/health"), { status: 200, body: { ok: true } });
});

test("a ticket is created, read, listed and closed over HTTP", async () => {
  const made = await call("POST", "/tickets", { title: "the invoice is wrong", description: "" });
  assert.equal(made.status, 201);
  assert.equal(made.body.category, "Billing");
  const read = await call("GET", `/tickets/${made.body.id}`);
  assert.equal(read.body.title, "the invoice is wrong");
  const list = await call("GET", "/tickets");
  assert.equal(list.body.tickets.length, 1);
  const closed = await call("PATCH", `/tickets/${made.body.id}`, { status: "closed" });
  assert.equal(closed.body.status, "closed");
});

test("a ticket without a title is refused", async () => {
  assert.deepEqual(await call("POST", "/tickets", {}), {
    status: 400,
    body: { error: "title_required" },
  });
});

test("an unknown ticket is a 404", async () => {
  assert.equal((await call("GET", "/tickets/42")).status, 404);
});
