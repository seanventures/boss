// DELIBERATELY VULNERABLE (the F4 security-scan proof, tests/e2e/security/README.md). Planted:
//   V1 SQL injection in findUser (string-built query)       -> Semgrep
//   V2 hardcoded JWT signing secret in issueToken            -> Semgrep
//   V3 a dependency with known advisories (lodash 4.17.20)   -> OSV-Scanner
//   V4 no security headers, X-Powered-By on                  -> ZAP baseline
const express = require("express");
const initSqlJs = require("sql.js");
const jwt = require("jsonwebtoken");
const _ = require("lodash");

const app = express();
const PORT = process.env.PORT || 3000;
let db;

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
  const rows = db.exec("SELECT id, name, email FROM users WHERE name = '" + req.query.name + "'");
  res.json(rows.length ? rows[0].values : []);
}

function issueToken(req, res) {
  const token = jwt.sign({ sub: req.query.user }, "s4-proof-hardcoded-signing-key", { expiresIn: "1h" });
  res.json({ token: token, roles: _.uniq(["reader", "reader"]) });
}

app.get("/", home);
app.get("/users", findUser);
app.get("/token", issueToken);

openDb().then((conn) => {
  db = conn;
  app.listen(PORT, process.env.HOST || "127.0.0.1", () => console.log("listening on " + PORT));
});
