# Add the Hello endpoint

Add one route to this app: `GET /hello` answers `200` with the JSON body
`{"message": "<greeting>"}`, where `<greeting>` is the `HELLO_GREETING` setting
(default `hello from BOSS`). Any other method on `/hello` answers `405`.

Use the reference implementation for this app's stack:

- **node**: put `hello.js` at the app's root (next to its `package.json`) and route `GET /hello`
  to its `handleHello(req, res)` from the app's existing server. Copy
  `tests/template_hello/hello.test.mjs` to the same path in the app.
- **python**: put `hello.py` at the app's root and route `GET /hello` to `hello_app` (a WSGI app)
  or call `greeting()` from the app's framework. Copy `tests/template_hello/test_hello.py` to
  the same path in the app.

Keep the template's tests as they are: they are the proof this template was applied.
