Northgate API Gateway: automate tenant API onboarding

Today onboarding a new tenant to our API gateway is manual. We want BOSS to run it: every tenant config change is validated (format, security, API guidelines), rejected with a report and a suggested fix when it doesn't conform, then deployed to staging, contract-tested, approved, and promoted to production.

This ticket builds the gateway and its validator (gwlint), the system the onboarding workflow runs against. Product name: Northgate API Gateway. Demo tenants are fictional: Lumen Pay, Orbit Travel, Harbor Health.

Stack: Go. (gateway and gwlint: one Go module, static binaries)

## What to build

### 1. `northgate-gateway` (Go, medium complexity)

A reverse-proxy API gateway, one static binary, configured by YAML.

- **Routing:** by host, path prefix and method to named upstreams; path rewrite.
- **Multi-tenant config:** `config/gateway.yaml` (global) + `config/tenants/<tenant>.yaml` (one per onboarded customer). **Hot reload:** watch the directory; validate the new set; swap atomically; keep the last good config on error and log why.
- **Authentication per route:** JWT/OAuth2 (issuer, audience, required scopes, JWKS URL with caching), API keys (hashed in config, never plaintext), optional mTLS client certificates. A route with no auth is a configuration error unless explicitly `public: true` with a reason.
- **Rate limiting:** token bucket per tenant and per route (requests/second + burst).
- **CORS** per route; **request size limits**; **request validation** against a JSON Schema or OpenAPI fragment per route.
- **Resilience:** upstream timeouts, retries with backoff for idempotent methods, a simple circuit breaker.
- **Headers:** add/remove/rename; propagate a request id.
- **Observability:** structured JSON access logs with PII redaction (configurable field list), `/healthz`, `/readyz`, Prometheus `/metrics`.
- **Two environments:** `staging` and `production` are two instances of the same binary with different config directories and ports. Provide `docker-compose.yml` for both plus two tiny mock upstreams so demos run end to end. (Do not depend on anything external.)
- **Tests:** unit tests for routing, auth, rate limits, reload; an integration test that boots the gateway with the sample config and exercises a request through auth → rate limit → upstream.

### 2. Tenant onboarding YAML: schema + guidelines + `gwlint`

- **JSON Schema** for `tenants/<tenant>.yaml` (tenant id, owner + contact, tier, routes, auth, limits, CORS, upstreams, deprecation).
- **A rulebook, in YAML, readable by non-engineers:** `rules/api-guidelines.yaml`. Each rule has `id`, `severity` (error|warn), `title`, `why`, `fix`. At least 20 rules:
  - **Security:** every route has auth (or an explicit, justified `public`); no wildcard CORS origin together with credentials; upstreams must be `https://`; no secrets inlined (detect API-key/JWT/password-shaped values); rate limits present and within bounds; no `/admin`, `/internal`, `/debug` paths exposed; JWT `audience` set; mTLS required for `tier: partner-sensitive`; request size capped; PII fields declared for redaction; no `*` methods.
  - **API guidelines:** versioned base path (`/v1/...`); kebab-case path segments; only allowed methods; timeouts within bounds; owner and contact present and well-formed; description present; deprecations carry a `sunset` date; consistent naming between tenant id and file name.
- **`gwlint` CLI** (Go, same repo): `gwlint validate <file|dir>` prints a human report (rule id, YAML line, why, how to fix) or `--json`; exit code **0** pass, **1** violations, **2** invalid YAML/schema. `gwlint --fix <file>` writes a corrected file for the mechanically fixable rules and lists the ones that need a human. Golden tests: one good tenant config; several bad ones each triggering specific rules; a `--fix` round-trip test.

### 3. Demo content

`examples/`: `lumen-pay.good.yaml`, `lumen-pay.bad.yaml` (at least 5 violations: a wildcard CORS with credentials, an http upstream, an inlined API key, a missing rate limit, an `/admin` route), and the README that tells a tenant how to onboard (fork/branch → add `tenants/<tenant>.yaml` → open a PR → BOSS validates).

## Quality bar

- Clean, idiomatic Go; `go vet` and `go test ./...` clean; `make test`, `make run-staging`, `make run-prod`.
- A README with: what it is, a 5-minute quick start, the YAML reference, the rulebook table (generated from the YAML), and how `gwlint` is meant to run in CI/BOSS.
- Plain, concise naming. Someone at the customer will read this code.
- No external services required to run the demo.

## Acceptance (what we will check)

1. `make run-staging` starts the gateway + mock upstreams; a request with a valid JWT reaches the upstream; without it gets 401; the 11th request in a second gets 429.
2. Editing `config/tenants/lumen-pay.yaml` is picked up without restart; a broken edit is rejected and the previous config keeps serving.
3. `gwlint validate examples/lumen-pay.bad.yaml` exits 1 and names each violation with a line number and a fix; `gwlint --fix` produces a file that then passes except for the rules that need a human, which are listed.
4. `gwlint validate examples/lumen-pay.good.yaml` exits 0.
5. All tests pass; the README quick start works as written.
