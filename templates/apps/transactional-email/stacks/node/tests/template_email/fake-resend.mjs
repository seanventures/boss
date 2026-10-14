// An in-process fake of Resend's POST /emails, with its Idempotency-Key behaviour: a repeated key
// answers with the first email's id and sends nothing new. No test reaches Resend.
import http from "node:http";
import crypto from "node:crypto";

// A key made per test run that says what it is; never a real credential.
export function fakeResendKey() {
  return "re_fake_" + crypto.randomBytes(12).toString("hex");
}

export async function startFakeResend({ apiKey }) {
  const state = { emails: [], requests: [], answerNext: [], byIdempotencyKey: new Map() };
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      state.requests.push({ method: req.method, url: req.url, headers: req.headers });
      const send = (status, body) => {
        res.writeHead(status, { "Content-Type": "application/json" });
        res.end(JSON.stringify(body));
      };
      if (req.headers.authorization !== `Bearer ${apiKey}`) return send(401, { name: "missing_api_key", statusCode: 401 });
      if (req.method !== "POST" || req.url !== "/emails") return send(404, { name: "not_found", statusCode: 404 });
      const forced = state.answerNext.shift();
      if (forced) return send(forced, { name: forced === 422 ? "validation_error" : "internal_server_error", statusCode: forced });
      const key = req.headers["idempotency-key"];
      if (key && state.byIdempotencyKey.has(key)) return send(200, { id: state.byIdempotencyKey.get(key) });
      const body = JSON.parse(raw);
      const id = crypto.randomUUID();
      state.emails.push({ id, key, body });
      if (key) state.byIdempotencyKey.set(key, id);
      return send(200, { id });
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return {
    state,
    url: `http://127.0.0.1:${server.address().port}`,
    close: () => new Promise((resolve) => server.close(() => resolve())),
  };
}
