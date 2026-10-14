# Email (SMTP or Resend): setup

The build and the preview work without any of this: with no provider set, every email the app
sends is kept in its mail catcher. Open `{{preview_url}}/dev/mail` to read them and follow their
links. Do these steps when you want real email to reach people.

BOSS sets `APP_BASE_URL` to the address people use, with no trailing slash: the preview's
(`{{preview_url}}`) in a preview, and the deployed address (`{{deploy_url}}`) on a deploy. Reset
and invite links are built from it.

## Resend

1. In Resend, **Domains > Add domain**, and add the DNS records it shows until the domain is
   **Verified**.
2. **API Keys > Create API key**, permission **Sending access**, limited to that domain.
3. Paste the key into the app's secret `RESEND_API_KEY` (App > Setup > Secrets). Paste it into
   BOSS only; never into code.
4. Set `EMAIL_FROM` to a sender on the verified domain, for example
   `Your App <no-reply@your-domain>`.

## SMTP

Any SMTP server works: your mail host's, or a provider's relay (Resend's is `smtp.resend.com`).

1. Set `SMTP_HOST`, `SMTP_PORT` (587 for STARTTLS, 465 with `SMTP_TLS=implicit`) and `SMTP_USER`.
2. Paste the password into the app's secret `SMTP_PASSWORD`.
3. Set `EMAIL_FROM` to a sender the server is allowed to send as.
4. The connector declares only Resend's hosts (`api.resend.com`, `smtp.resend.com`) as the
   hosts the app talks to. Another SMTP server must be reachable from where the app runs; where
   the app's outbound traffic is limited to the hosts approved at apply, use Resend (its API or
   its SMTP relay).

`SMTP_TLS=none` is for a local catcher such as Mailpit during development; a production app
refuses to start with it.

## Production

In production (`NODE_ENV=production`, which every BOSS deploy sets) the mail catcher never runs.
With no provider set the app still starts, `GET /api/email/status` says `"provider": "off"`, and
every send fails visibly (`email_not_configured`): set Resend or SMTP before you rely on resets
or invites.

## Check it

1. Open `{{preview_url}}/api/email/status`: it says which way mail goes (`catcher`, `smtp`,
   `resend` or `off`) and counts sent and failed messages.
2. Ask for a password reset from the app's sign-in page, then open `{{preview_url}}/dev/mail`
   (catcher) or your inbox (a provider) and follow the link.
3. A message that failed is counted under `outbox.failed`; the app's log names its code
   (`resend_auth_failed`, `smtp_rejected`, ...). A provider that rejects the sender usually means
   `EMAIL_FROM` is not on a verified domain.
