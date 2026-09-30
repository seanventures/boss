# Call the API from another machine

Publish the BOSS API beyond loopback so scripts and tools on other machines can call it, while keeping authentication on and the database private.

## Publish the API port

By default the installer binds the API to `127.0.0.1`, which answers only on the install machine. To publish it, pass `--api-bind` with the address to listen on, such as one interface on that machine or `0.0.0.0` for all interfaces. Without the flag, the bind stays on `127.0.0.1`. See [Publish the API for remote clients](hostname.md#publish-the-api-for-remote-clients) for the accepted values.

Postgres always stays on loopback. The API and worker reach it over the install's private network, so never publish the database port.

## Authenticate every call

Every remote call needs the API key from the `.env` on the install machine. `BOSS_REQUIRE_API_AUTH` stays on, and calls with a missing or wrong key are rejected. The `Host` header never substitutes for the key, even when it names a machine other than loopback.

Send the key in a header, as either `Authorization: Bearer` or `X-Boss-Api-Key`.

!!! warning
    Never put the key in a URL, a query string, or a command-line argument. Those end up in shell history, proxy logs, and process listings.

On the install machine, running `source ./boss-community.env.sh` from the install directory loads `BOSS_API_KEY` into your shell. Pass that variable only in the header:

```bash
curl -fsS -H "Authorization: Bearer $BOSS_API_KEY" \
  "https://boss.example/v1/edition?project_id=$BOSS_PROJECT_ID"
```

Here `boss.example` stands for the HTTPS hostname you serve.

## Add TLS with your own proxy

BOSS does not terminate TLS on a published address. If the API port is reachable from outside a network you trust, put a TLS proxy you operate in front of it and point clients at the proxy. Keep Postgres on loopback either way.

## Reach the API without publishing it

While the API stays on loopback, an SSH tunnel to `127.0.0.1` still reaches it. The console uses a separate listener; see [console access](CONSOLE_AUTH.md) for how to reach and sign in to it.
