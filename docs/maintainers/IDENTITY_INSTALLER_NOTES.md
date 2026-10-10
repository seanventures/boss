# Identity: installer follow-ups

For the owner of `install.sh`. Named accounts (Identity) ship in v1.0-rc6 without touching `install.sh`; these are the installer changes that would finish the job. Line numbers are against the branch this note was written on (`id/fx-a`). Each item: what, where, why, and a suggested change.

## (a) `BOSS_PUBLIC_URL` means two different things

**What.** The installer and `checkup.sh` treat `BOSS_PUBLIC_URL` as the address a browser uses to reach the **API** (for example `http://host:8000`). Identity treats it as the **console's** public origin.

**Where.**

- `install.sh:136-147`: `PUBLIC_URL="${BOSS_PUBLIC_URL:-}"`, documented as the address compiled into the web bundle as `NEXT_PUBLIC_BOSS_API_URL`.
- `install.sh:281`: listed among the env overrides.
- `install.sh:1056-1063`: completed to `http://$PUBLIC_URL:${API_PORT}` (an API URL).
- `scripts/install/checkup.sh:109`, `:189-192`: read as the API address the console was built with.
- `engine/boss_engine/config.py:1449` (`public_url`), `engine/boss_engine/identity/claim.py:186`, `:282`: identity's public origin. It sets sign-in cookies, the WebAuthn RP ID (passkeys), CSRF checks, SSO redirect URLs and every emailed link, and always wins over the origin bound at the claim.
- `scripts/install/community-compose.yml:140`, `:377`, `:493`: `BOSS_PUBLIC_URL: ${BOSS_PUBLIC_URL:-${BOSS_CONSOLE_PUBLIC_ORIGIN:-}}` passes it to api, worker and egress **ahead of** `BOSS_CONSOLE_PUBLIC_ORIGIN`.

**Why it matters.** An operator who follows the installer's env-override docs, or exports `BOSS_PUBLIC_URL` in the shell that runs `boss upgrade`, binds identity to the API port: passkeys registered against it fail on the console, emailed links point at the API, and SSO redirects break.

**Suggested change.** Rename the installer's variable to `BOSS_API_PUBLIC_URL` (keep reading `BOSS_PUBLIC_URL` for one release with a warning, and stop writing it), so `BOSS_PUBLIC_URL` means only the console origin. Or, smaller: stop passing `BOSS_PUBLIC_URL` to identity in compose and pass only `BOSS_CONSOLE_PUBLIC_ORIGIN`. The root `README.md` now says `--console-origin` writes `BOSS_CONSOLE_PUBLIC_ORIGIN` (it used to say `BOSS_PUBLIC_URL`).

## (b) The database password fallback

**What.** `install.sh:698-701` sets `BOSS_DB_PASSWORD="boss"` and replaces it with a random value only when `openssl` exists. Compose now refuses an unset value (`community-compose.yml:22`, `:71`, `:124`, `:320`, `:491`: `${BOSS_DB_PASSWORD:?…never the old default}`), but a host without `openssl` still writes the literal `boss`.

**Why it matters.** Identity stores password hashes, TOTP secrets (encrypted), API key hashes, sessions and the hash-chained audit log in that database. A guessable database password next to a published Postgres port, or on a shared Docker network, undoes all of it.

**Suggested change.** Generate it without `openssl` (`head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n'`, as `install.sh:695-696` already does for the API keys) and never write `boss`. On reruns, warn when the preserved value is `boss` and offer a rotation (`ALTER ROLE` plus `.env`).

## (c) A runtime database role that does not own the schema

**What.** The api and worker connect as `boss`, the `POSTGRES_USER` of the stock compose (`community-compose.yml:21`), which owns every table and is a superuser.

**Where.** `db/schema.sql:7525-7560`: the audit log is append-only by trigger, and the only deletion runs inside `SECURITY DEFINER` functions owned by the `NOLOGIN` role `boss_audit_owner`. The comment there says it plainly: when the app's role is a superuser, privileges cannot bind it; only the triggers and the keyed hash chain remain.

**Why it matters.** A superuser app role can drop the trigger. Posture reports this, but the protection the schema was designed for needs the app to run as a role that owns nothing.

**Suggested change.** At install, create a `boss_app` login role with `CONNECT`, `USAGE` on the schema, and `SELECT/INSERT/UPDATE/DELETE` on tables (`INSERT/SELECT` only on `identity_security_events`), and point the api and worker `DATABASE_URL` at it; keep `boss` (owner) for migrations only (`ensure_schema` at boot would need the owner URL, or a separate migrate step). `scripts/install/initdb/` is the place for the role (it holds only `10-infisical.sql` today).

## (d) Print the claim link at the end of an install

**What.** On a fresh install the next step is the claim, and today the only place the claim link appears unprompted is the API container's log (`[identity] this BOSS has no Owner yet. Claim it within 1 hour: …`, printed at boot by `engine/boss_engine/identity/claim.py:announce_setup_link`). The installer's final summary (`install.sh` ~1600-1670) prints the password command and `boss open`, but not the claim. On 2026-10-10 a first-run operator could not claim without reading `docker logs`.

**What BOSS already does (no installer change needed for these).** `boss open` and `boss doctor` say "unclaimed" and print `boss identity setup-link`; the console's sign-in page shows the same command under "This BOSS has no Owner yet"; `/claim` without a link shows it with a copy button.

**Suggested change.** At the very end of a FIRST install (not a rerun or an upgrade), once the api is healthy, mint the link and print it as the last thing on screen:

```bash
if [[ "$(docker compose … exec -T -u boss api python -m boss_engine.identity.cli claim-status | tail -n 1)" == unclaimed ]]; then
  printf '\n  Claim it (one hour, single use; open it in the browser you will sign in with):\n\n'
  bash "$INSTALL_DIR/scripts/install/boss.sh" --dir "$INSTALL_DIR" identity setup-link | sed 's/^/    /'
  printf '\n  Link expired? Run: boss identity setup-link\n'
fi
```

The link's token is in the URL fragment, so it never reaches an access log; it is printed to the terminal of the person who just ran the installer, which is the proof the claim needs. When the install has a console origin (`--console-origin`), the link uses it; otherwise it is `http://localhost:3000/claim#…`, which is right for a laptop and for an SSH tunnel.

## (e) `boss-community.env.sh` exports the shared key

**What.** `install.sh:1156-1162` writes `export BOSS_API_KEY="${BOSS_API_KEY}"`: the shared key, which loses people-and-access changes at the claim and stops 30 days later.

**Suggested change.** Export the host tools' key instead, read from `.env` when the file is sourced so a rotation is picked up:

```bash
export BOSS_API_KEY="$(sed -n 's/^BOSS_HOST_API_KEY=//p' "$INSTALL_DIR/.env")"
```

and issue it during install if missing (`bash scripts/install/boss.sh --dir "$INSTALL_DIR" identity host-key`). The docs currently tell people to add that line themselves.

## (f) Pick the small password-hashing profile on small hosts

**What.** Password hashing defaults to argon2id with 64 MiB per hash (`BOSS_ARGON2_PROFILE=default`); up to four run at once. `.env.example` documents `small` (19 MiB) for hosts under 2 GiB, but the installer never sets it.

**Suggested change.** When total memory is under 2 GiB (the installer already measures the host), write `BOSS_ARGON2_PROFILE=small` to `.env` on first install, and keep a previous value on reruns.

## (g) Keep `BOSS_HOST_API_KEY` across reruns

**What.** The host tools' key lives in `.env` as `BOSS_HOST_API_KEY`. The installer rewrites `.env` from a heredoc.

**Status.** `scripts/install/restore_preserved_env.sh` already lists `BOSS_HOST_API_KEY` in `KEEP_PREVIOUS`, so a rerun keeps it. If it were lost, `boss` issues a new one on the next command. Nothing to change in `install.sh`; noted so a future refactor of the preserved-lines list keeps it.
