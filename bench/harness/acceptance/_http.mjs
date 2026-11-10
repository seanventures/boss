// Shared by the benchmark's held acceptance tests for the HelpDesk tasks: the service's own
// handler on an ephemeral loopback port, and a JSON call helper.
import { createServer } from "node:http";
import { handle } from "../src/app.js";

export async function startService() {
  const server = createServer(handle);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  async function call(method, path, body) {
    const res = await fetch(base + path, {
      method,
      headers: { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await res.text();
    let json = null;
    try {
      json = JSON.parse(text);
    } catch {
      json = null;
    }
    return { status: res.status, body: json };
  }
  return { call, close: () => new Promise((resolve) => server.close(resolve)) };
}
