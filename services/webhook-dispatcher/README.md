# webhook-dispatcher (BOSS-102 / S3)

Optional **Enterprise** high-fanout sidecar. Python still owns the event ledger,
subscriptions, HMAC secret *refs*, leased `webhook.outbound.deliver` jobs, and
Receipts. This binary only fans an already-built event envelope to webhook URLs.

**Status:** in-repo source, unit-tested, **not live-mounted**, **not Community GA**.
`s3_go_event_dispatcher` stays `false` until a live probe exists.

## Contract

- Consume a spec-00 event envelope (`event_id`, `event_type`, destinations).
- One `Receipt` + unique `dedupe_key` per destination. Never silent drop.
- HMAC `X-Hub-Signature-256` over the exact JSON body (`env:NAME` secret refs).
- Bounded retry, then `deadlettered`. Never invent success.
- Default **dry-run**: no outbound network. Set
  `BOSS_GO_WEBHOOK_DISPATCHER_DRY_RUN=false` only when you intend live POSTs.

## Run (local)

```bash
go test ./...
BOSS_GO_WEBHOOK_DISPATCHER_DRY_RUN=true go run ./cmd/webhook-dispatcher
```

`GET /health` and `POST /v1/dispatch` (optional `X-Boss-Dispatcher-Token`).

The engine works when this process is unset: the Python adapter returns
`not_configured` and the existing Python webhook path continues.

See `docs/operations/GO_SCALE_TRACK.md` and `docs/operations/WEBHOOKS.md`.
