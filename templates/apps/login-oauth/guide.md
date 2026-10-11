# Login with Google, GitHub or BOSS: setup

The build works without any of this: its tests sign in through a fake provider. Do these steps
when you want real people to sign in to the preview or the deployed app.

BOSS sets `APP_BASE_URL` to the address people use, with no trailing slash: the preview's
(`{{preview_url}}`) in a preview, and the deployed address (`{{deploy_url}}`) on a deploy. The redirect URIs below are built from it, and the session
cookie is marked `Secure` whenever it starts with `https://`.

## Google

1. Open the Google Cloud console, **APIs & Services > Credentials**, and choose
   **Create credentials > OAuth client ID**, type **Web application**.
   (First time in the project: configure the OAuth consent screen, user type External, and add
   yourself as a test user.)
2. Under **Authorized redirect URIs**, add:
   - `{{redirect_uri}}/google` (preview)
   - `{{deploy_url}}/auth/callback/google` (deploy)
3. Copy the client ID into the app's secret `GOOGLE_CLIENT_ID` and the client secret into
   `GOOGLE_CLIENT_SECRET` (App > Setup > Secrets). Paste them into BOSS only; never into code.

## GitHub

1. GitHub > **Settings > Developer settings > OAuth Apps > New OAuth App**.
2. **Homepage URL**: `{{preview_url}}`. **Authorization callback URL**: `{{redirect_uri}}/github`.
   GitHub allows one callback URL per OAuth app: create a second app for the deployed host,
   with **Homepage URL** `{{deploy_url}}` and callback `{{deploy_url}}/auth/callback/github`.
3. **Generate a new client secret**. Copy the client ID into `GITHUB_CLIENT_ID` and the secret
   into `GITHUB_CLIENT_SECRET`.

## Sign in with BOSS

When BOSS's OIDC provider is on for this install, registering the app there fills
`OIDC_BOSS_ISSUER`, `OIDC_BOSS_CLIENT_ID` and `OIDC_BOSS_CLIENT_SECRET`, with the redirect URI
`{{redirect_uri}}/boss`. Any other OpenID Connect provider with discovery works the same way:
set its issuer URL and client credentials in those three settings.

## Roles

Everyone who signs in has the role `user`. To make someone an `admin`, add their email to
`AUTH_ADMIN_EMAILS` (comma-separated); it applies only when their provider says the email is
verified.

## Check it

1. Open `{{preview_url}}/auth/providers`: it lists the providers whose client ID is set.
2. Open `{{preview_url}}/auth/login/google` (or `github`, `boss`), sign in, and you land back
   on the app.
3. `{{preview_url}}/api/me` shows who you are; signing out from the app's header ends the session.

A provider that says `redirect_uri_mismatch` is comparing against a URI that differs from
`APP_BASE_URL` + `/auth/callback/<provider>` by a character (http vs https, a trailing slash, a
different port): make them identical.
