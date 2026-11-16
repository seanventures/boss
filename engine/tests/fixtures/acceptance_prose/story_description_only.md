## Proposal: tenant usage report

Tenant owners cannot see how much of their rate limit they use today, so they open support tickets to ask. We want the gateway to answer that itself.

Add a `GET /v1/usage` endpoint that returns, for the calling tenant only, the requests made in the last 24 hours per route and the share of the rate limit used. The response must be JSON and must never include another tenant's numbers. The endpoint needs the same authentication as the tenant's routes.

Later we may chart this in a console, but that is not part of this story.
