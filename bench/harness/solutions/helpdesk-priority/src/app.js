// The HTTP routes. `handle(req, res)` is the whole service; server.js only listens.
import { createTicket, getTicket, listTickets, setStatus, ValidationError } from "./store.js";

function send(res, status, body) {
  res.statusCode = status;
  res.setHeader("content-type", "application/json");
  res.end(JSON.stringify(body));
}

async function readJson(req) {
  let raw = "";
  for await (const chunk of req) raw += chunk;
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export async function handle(req, res) {
  const url = new URL(req.url, "http://localhost");
  const parts = url.pathname.split("/").filter(Boolean);
  try {
    if (req.method === "GET" && url.pathname === "/health") return send(res, 200, { ok: true });
    if (parts[0] === "tickets" && parts.length === 1) {
      if (req.method === "GET") {
        const priority = url.searchParams.get("priority") ?? undefined;
        return send(res, 200, { tickets: listTickets({ priority }) });
      }
      if (req.method === "POST") {
        const body = await readJson(req);
        if (body === null) return send(res, 400, { error: "invalid_json" });
        return send(res, 201, createTicket(body));
      }
    }
    if (parts[0] === "tickets" && parts.length === 2) {
      if (req.method === "GET") {
        const ticket = getTicket(parts[1]);
        return ticket ? send(res, 200, ticket) : send(res, 404, { error: "not_found" });
      }
      if (req.method === "PATCH") {
        const body = await readJson(req);
        if (body === null) return send(res, 400, { error: "invalid_json" });
        const ticket = setStatus(parts[1], body.status);
        return ticket ? send(res, 200, ticket) : send(res, 404, { error: "not_found" });
      }
    }
    return send(res, 404, { error: "not_found" });
  } catch (err) {
    if (err instanceof ValidationError) return send(res, 400, { error: err.code });
    return send(res, 500, { error: "internal" });
  }
}
