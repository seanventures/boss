# FXQuote: Go foreign-exchange quote service for a fictional bank

Provide real-time foreign-exchange quotes for a fictional bank's internal/demo use.

This project's code lives on this machine, in a git repository BOSS keeps. Publish it to
GitHub whenever you are ready; its whole history goes with it.

## Running locally

```
go run ./cmd/fxquote
```

Rates are loaded from `rates.yaml` (override with `FXQUOTE_CONFIG`). The server binds
`HOST`/`PORT` (defaults `0.0.0.0:8080`).

## API

### Happy path

```
curl "http://localhost:8080/quote?from=USD&to=EUR&amount=10000"
# {"id":"q_...","from":"USD","to":"EUR","amount_in":10000,"amount_out":9054,"rate":"0.9055","expires_at":"..."}
```

### Cross rate, derived via USD (no direct EUR/GBP pair configured)

```
curl "http://localhost:8080/quote?from=EUR&to=GBP&amount=10000"
```

### 400: unknown currency

```
curl -i "http://localhost:8080/quote?from=USD&to=ZZZ&amount=10000"
# 400 {"reason":"unknown_currency"}
```

### 400: non-positive / malformed amount

```
curl -i "http://localhost:8080/quote?from=USD&to=EUR&amount=0"
# 400 {"reason":"non_positive_amount"}
curl -i "http://localhost:8080/quote?from=USD&to=EUR&amount=notanumber"
# 400 {"reason":"malformed_amount"}
```

### 400: currency with no USD-reachable rate

```
curl -i "http://localhost:8080/quote?from=USD&to=JPY&amount=10000"
# 400 {"reason":"no_usd_rate"}   (when JPY is only configured against EUR, not USD)
```

### 404: unknown/never-issued quote id

```
curl -i "http://localhost:8080/quote/never-issued"
# 404
```

### 410: expired quote id

```
curl -i "http://localhost:8080/quote/<id-from-earlier-response>"
# 410, once more than 30s have passed since issuance
```

### Health, readiness, metrics

```
curl http://localhost:8080/healthz
curl http://localhost:8080/readyz
curl http://localhost:8080/metrics
```

## Limitations (by design — see PRD)

- Single instance, in-memory rate table and quote store. A restart wipes all issued quotes;
  a lookup for a pre-restart quote id is indistinguishable from one never issued (404).
- No auth, no app-level rate limiting. Default deployment target is internal-only; any
  exposure beyond that requires a separate security review.

## Tests

```
go test ./...
```

Covers rounding (half-even), spread application, cross-rate derivation via USD, validation
order (currency/amount checked before any rate math), quote id uniqueness at 10k scale, the
404-vs-410 boundary, and the platform metrics contract.
