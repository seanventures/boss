// The F4 security-scan proof's app AFTER the fix (tests/e2e/security/README.md):
//   V1 findUser uses a bound parameter; V2 the signing key comes from the environment;
//   V3 lodash is upgraded; V4 security headers are set and X-Powered-By is off.
// Every handler keeps its name, and lines move: findings that remain must keep their fingerprints.
const express = require("express");
const initSqlJs = require("sql.js");
const jwt = require("jsonwebtoken");
const _ = require("lodash");

const app = express();
const PORT = process.env.PORT || 3000;
const SIGNING_KEY = process.env.TOKEN_SIGNING_KEY || require("crypto").randomBytes(32).toString("hex");
let db;

app.disable("x-powered-by");
app.use((req, res, next) => {
  res.set("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; form-action 'self'");
  res.set("X-Content-Type-Options", "nosniff");
  res.set("X-Frame-Options", "DENY");
  res.set("Cross-Origin-Resource-Policy", "same-origin");
  res.set("Permissions-Policy", "camera=(), microphone=(), geolocation=()");
  res.set("Cache-Control", "no-store");
  next();
});

async function openDb() {
  const SQL = await initSqlJs();
  const conn = new SQL.Database();
  conn.run("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)");
  conn.run("INSERT INTO users (name, email) VALUES ('alice', 'alice@example.test'), ('bob', 'bob@example.test')");
  return conn;
}

function home(req, res) {
  res.send("<html><head><title>Users</title></head><body><h1>Users</h1>" +
    "<a href=\"/users?name=alice\">alice</a> <a href=\"/token?user=alice\">token</a></body></html>");
}

function findUser(req, res) {
  const stmt = db.prepare("SELECT id, name, email FROM users WHERE name = :name");
  const rows = [];
  stmt.bind({ ":name": String(req.query.name || "") });
  while (stmt.step()) rows.push(Object.values(stmt.getAsObject()));
  stmt.free();
  res.json(rows);
}

function issueToken(req, res) {
  const token = jwt.sign({ sub: req.query.user }, SIGNING_KEY, { expiresIn: "1h" });
  res.json({ token: token, roles: _.uniq(["reader", "reader"]) });
}

app.get("/", home);
app.get("/users", findUser);
app.get("/token", issueToken);

openDb().then((conn) => {
  db = conn;
  app.listen(PORT, process.env.HOST || "127.0.0.1", () => console.log("listening on " + PORT));
});
