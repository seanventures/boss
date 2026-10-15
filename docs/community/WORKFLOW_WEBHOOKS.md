# Starting a workflow from a webhook

Another system (a form, a shop, a payment provider, a CI job) can start one of your workflows by
sending it signed JSON. Each workflow can have any number of webhook triggers. Each trigger has:

- **a URL** of the form `/v1/webhooks/inbound/workflow/<token>` on your BOSS API's public address.
  The token is random. It finds the trigger, but it does not authenticate the request.
- **a signing secret** (`whsec_…`). BOSS shows it once, when you create the trigger, and once
  more each time you rotate it. It is kept in the install's secret store, not in the database
  row, an event or a log.

A delivery that is correctly signed starts a run of the workflow's current published version. The
JSON body becomes the run's input, with three fields added: `trigger` (`"webhook"`),
`webhook_trigger_id` and `delivery_id`. If the body has a field with the same name, the body's
value is used. This is the same rule that applies when a BOSS event starts a workflow.

## Set one up

In the designer, open the workflow and click an empty part of the canvas to show **Settings**.
Then, under **Webhooks**:

1. Click **+ Webhook**. Copy the secret now, because it is not shown again. Click **Done**.
2. Click **Copy URL**. This copies the path. Put it after your BOSS API's public address
   (for example `https://boss-api.example.com`), not the console's. The console's `/v1`
   pass-through drops `X-BOSS-*` headers, so a signature sent through it never arrives.
3. **How to sign** shows a `curl` command that signs and sends one delivery.

You can do the same through the API. These routes need `workflow_schedule:manage` to change
anything and `workflow:read` to list:

| Method and path | What it does |
|---|---|
| `GET /v1/delivery-workflows/webhooks?project_id=&workflow_key=` | Lists the triggers. The secret is never included. |
| `POST /v1/delivery-workflows/webhooks` `{project_id, workflow_key, name}` | Creates a trigger. **The response has `secret`.** |
| `POST /v1/delivery-workflows/webhooks/{id}/rotate` `{project_id}` | Makes a new secret, returned once. The old one stops working at once. |
| `POST /v1/delivery-workflows/webhooks/{id}/enabled` `{project_id, enabled}` | Turns the trigger off or on. |
| `GET /v1/delivery-workflows/webhooks/{id}/deliveries?project_id=` | Lists recent deliveries and what happened to each. |

## How to sign a delivery

Send `POST` with `Content-Type: application/json` and these headers:

| Header | Value |
|---|---|
| `X-BOSS-Timestamp` | The current time in Unix seconds. |
| `X-BOSS-Signature` | `v1=` followed by the lower-case hex HMAC-SHA256 of `<timestamp>.<raw body>`, keyed with the secret. |
| `X-BOSS-Delivery` | Optional. Your own id for this delivery. BOSS uses it to deduplicate. |

The signed message is the timestamp, a dot, and the **exact bytes** you send as the body. If you
sign one serialisation of the JSON and send a different one, the signature does not match.

```sh
export BOSS_WEBHOOK_SECRET=whsec_...       # from the trigger; do not paste it into the command
BODY='{"company": "Acme", "plan": "pro"}'
TS=$(date +%s)
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$BOSS_WEBHOOK_SECRET" | sed 's/^.* //')
curl -sS -X POST "https://boss-api.example.com/v1/webhooks/inbound/workflow/<token>" \
  -H 'Content-Type: application/json' \
  -H "X-BOSS-Timestamp: $TS" \
  -H "X-BOSS-Signature: v1=$SIG" \
  -H "X-BOSS-Delivery: $(uuidgen)" \
  --data-binary "$BODY"
```

The same signature in Python:

```python
import hashlib, hmac, json, time

body = json.dumps({"company": "Acme"}).encode()
ts = str(int(time.time()))
sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
headers = {"Content-Type": "application/json", "X-BOSS-Timestamp": ts,
           "X-BOSS-Signature": f"v1={sig}", "X-BOSS-Delivery": "order-1234"}
```

While you change over to a rotated secret, a sender can send both signatures separated by a
comma (`v1=<old>,v1=<new>`). BOSS accepts the delivery if either one matches the current secret.

## What BOSS answers

| Status | Meaning |
|---|---|
| `202` `{"status": "started", "run_id": …}` | A run was started. |
| `200` `{"status": "duplicate", "run_id": …}` | This delivery was already accepted, so no second run was started. |
| `401` `bad_signature`, `stale_timestamp`, `missing_signature` | The signature or timestamp is wrong, or missing. |
| `403` `actor_not_authorized` | The trigger's creator can no longer start this workflow (see below). |
| `409` `disabled`, `not_ready`, `workflow_missing` | The trigger is off, the project lacks something the workflow needs, or the workflow has no published version. |
| `413` `too_large` | The body is larger than 256 KiB. |
| `415` `unsupported_content_type` / `400` `invalid_json` | The body is not a JSON object. |
| `429` `rate_limited` | The trigger has started 60 runs in the last minute. |
| `404` | No trigger has this URL. BOSS says nothing more than that. |

## Replays and redeliveries

- **Timestamp window.** The timestamp is part of the signature, and BOSS refuses a delivery whose
  timestamp is more than 5 minutes from its own clock in either direction. A captured request
  cannot be replayed after that.
- **Deduplication.** The dedupe key is `X-BOSS-Delivery` when you send it. Otherwise it is the
  hash of the signed timestamp and body. A redelivery with the same key gets `200 duplicate` and
  the original run id, and no new run starts. A refused delivery does not use up its key, so you
  can send it again after you fix the problem (for example, after you turn the trigger back on).

## Every delivery is recorded

Each delivery that reaches a trigger is recorded with its outcome: started, duplicate, or refused
with its reason. You can see these under **Deliveries** in the designer and from the deliveries
route. The body is never stored, only its size and SHA-256. Each trigger itemises at most 30
refused deliveries a minute. Past that limit, refusals are counted on the trigger
("+N more refused"), so a flood of bad requests cannot fill the table but still shows up. The
event feed also records `workflow.webhook_created`, `workflow.webhook_secret_rotated`,
`workflow.webhook_enabled`/`workflow.webhook_disabled` and `workflow.webhook_delivered`, and a
`workflow.start_refused` with the missing items when the workflow is not ready.

## Whose authority a delivery runs on

A delivery starts its run as the member who created the trigger. BOSS checks that member's
authority again for **every** delivery, not only when the trigger was created. It does this only
after the signature has been verified:

- **Standing grant.** When identity is enforced, creating a trigger links the creator's standing
  grant for the workflow, in the same way that saving a schedule does. If that person has no such
  grant, BOSS asks them for one. At each delivery the grant must still be live and still belong to
  that person. If it has expired or been revoked, or if no grant was ever linked, the delivery is
  refused with `actor_not_authorized`. BOSS also **turns the trigger off** and shows
  "Paused: renew authority" (the trigger's `last_error`), the same way a schedule pauses. To
  resume, the owner turns the trigger back on, which links their current standing grant.
- **Permission to run workflows.** The creator must still be able to start workflows in the
  project. When identity is enforced, that means `workflow_run:start` at the project through their
  role bindings, with an active account. Otherwise it means the `workflow_execute` member
  permission. If the creator has been removed from the project or has lost that permission, the
  delivery is refused with `actor_not_authorized` and no run starts. The trigger **stays on**, so
  restoring the member's access is enough for the next delivery to start a run.

Each of these refusals answers `403` with `{"status": "refused", "reason": "actor_not_authorized"}`.
It is recorded in the trigger's deliveries, and the event feed records a
`workflow.webhook_refused_authority` event that says whether the trigger was paused. The
creator also needs permission to run workflows when they create the trigger or turn it back on.
