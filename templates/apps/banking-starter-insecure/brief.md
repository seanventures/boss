# Create the banking starter (training variant)

This is a **training app** for practising security review inside BOSS's private preview. It is
labelled `insecure_by_design`: it is applied only to a new lab project and is never deployed to
a host.

Reproduce the Java/Dropwizard reference implementation **as written**. Do not harden, refactor,
"fix" or add checks to it: the review exercise that follows depends on the code behaving exactly
like the reference. Keep:

- the routes in the manifest, the request and response shapes, and the status codes;
- the red "Insecure by design — training only" banner on `GET /` and the
  `X-Insecure-By-Design: training only` header on every response;
- `GET /openapi.json` and `GET /health`;
- seeding of `BOSS_TEST_USERS` when `APP_ENV` is not `production`.

Use the reference for this app's stack:

- **java** (Dropwizard 4, JDBI 3, SQLite): the reference is a whole project (`pom.xml`,
  `config.yml`, `src/main/java/bank/...`, `src/main/resources/openapi.json`); use it as the
  project root. Keep `<testSourceDirectory>tests</testSourceDirectory>` and copy
  `tests/bank/*.java` unchanged: they check that the bank works and are the proof the template
  was applied. `config.yml` must keep `port: ${PORT:-8080}` so the preview binds.
