# Hello World

A tiny web app: one page that says hello and shows the current time. It is BOSS's sample
project, so keep it exactly this small.

- `GET /` answers `200` with an HTML page whose heading is `Hello, <name>!` (`HELLO_NAME`,
  default `World`) and which shows the server's current time (ISO 8601) under it.
- No dependencies: Node's own `http` module (`server.js`, listening on `PORT`, default `3000`).
- `npm start` runs it; `npm test` runs the one test.

Use the reference implementation as it is: put `server.js` and `package.json` at the app's root
and copy `tests/template_hello_world/hello-world.test.mjs` to the same path. Keep the template's
test as it is: it is the proof this template was applied.
