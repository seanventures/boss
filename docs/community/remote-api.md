# Call the API from another machine

The Community installer binds the API to `127.0.0.1` by default. That address answers only on the machine where BOSS is installed. Leaving the default in place does not publish the API.

`--api-bind` is being added to the installer. It publishes the API port on an address you choose, such as one interface on that machine or `0.0.0.0` for every interface. Omitting it keeps the bind on `127.0.0.1`. Publishing the port is not the default.

Postgres stays on loopback. API and worker reach it on the install's private network. Do not publish the database port.

## The API key is still required

Every remote call needs the API key from the local `.env` on the install machine. `BOSS_REQUIRE_API_AUTH` stays on. A call with no key is rejected. A call with the wrong key is rejected. The `Host` header does not replace the key, including when it names a machine other than loopback.

The key is a header. Send `Authorization: Bearer` or `X-Boss-Api-Key`. Do not put the key in a URL, a query string, or a command-line argument. Those land in shell history, proxy logs, and process listings.

On the install machine, `source ./boss-community.env.sh` from the install directory loads `BOSS_API_KEY` into that shell. Pass that variable only in the header:

```bash
curl -fsS -H "Authorization: Bearer $BOSS_API_KEY" \
  "https://boss.example/v1/edition?project_id=$BOSS_PROJECT_ID"
```

`boss.example` stands for the HTTPS name you serve. It is not a host run for you.

## TLS is your proxy

BOSS does not terminate TLS for a public address. If the published API port is reachable past a network you trust, put a TLS proxy you operate in front of it and point clients at that proxy. The proxy is yours. Keep Postgres on loopback either way.

An SSH tunnel to `127.0.0.1` remains the way to reach an API you did not publish. The console listener is separate; sign-in for that listener is in [console access](CONSOLE_AUTH.md).

This page describes software you run on a machine you administer. It does not announce a hosted service or a public release.
