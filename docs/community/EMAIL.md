# Email

BOSS sends invitations, password resets, sign-in codes and workflow mail. Every install sends
**from its own domain, through its own provider**. BOSS ships no sender, no domain and no
provider key.

## Where mail goes

BOSS picks one route for every email, in this order:

| Route | When | Leaves the machine |
|---|---|---|
| **Settings › Email** | You saved a mail server in the console | Yes |
| **Environment** | `BOSS_EMAIL_SMTP_*` is set and `BOSS_EMAIL_REAL_SEND_ALLOWED=true` | Yes |
| **Dev inbox (Mailpit)** | No provider yet; installed with Mission Control | No |
| **None** | Nothing above | Invites show a link to copy instead |

A fresh install sends to the **dev inbox**. Open it at `http://localhost:8025` to read every
email BOSS sent, with no account anywhere. An invitation that lands there does not verify the
person's address, because anyone on the machine can read the dev inbox.

## Set up a provider

In the console, go to **Settings › Email** (or the *Outgoing email* item on a start checklist):

- **SMTP host** and **Port**: port 587 with STARTTLS is the usual setting. Use 465 for SSL.
- **Login** and **SMTP key**: use the SMTP credentials your provider gives you, not your
  account password. The key goes to BOSS's secret store and is never shown again.
- **Send as**: an address on a domain **you** verified with the provider, for example
  `boss@example.com`.

Press **Save mail server**, then **Send test**. If the test fails, BOSS shows the server's own
reply, for example `535 authentication failed` or `550 sender not allowed`.

### Example: Brevo

1. In Brevo, open **Senders, Domains & Dedicated IPs › Domains**, add `example.com`, and add the
   DNS records it lists (a Brevo code, DKIM, DMARC). Wait until it shows *Authenticated*.
2. Under **Senders**, add `boss@example.com`.
3. Open **SMTP & API › SMTP**. Note the **SMTP server** (`smtp-relay.brevo.com`), the port
   (587) and the **Login**. Press **Generate a new SMTP key** and copy the key once.
4. In BOSS, open **Settings › Email**. Enter host `smtp-relay.brevo.com`, port `587`, Send as
   `boss@example.com`, Login (from step 3) and SMTP key (from step 3). Press **Save mail server**,
   then **Send test**.
5. Invite someone under **People › Invite people**. The dialog shows *Sent*, or *Failed* with the
   reason, with **Resend** and **Copy link**.

### Other providers

| Provider | Host · port | Login · secret | Free tier (check the provider's current terms) |
|---|---|---|---|
| Brevo | `smtp-relay.brevo.com` · 587 | SMTP login · SMTP key | about 300 emails a day |
| Resend | `smtp.resend.com` · 587 | `resend` · API key | about 100 a day, 3,000 a month |
| Mailjet | `in-v3.mailjet.com` · 587 | API key · secret key | about 200 a day, 6,000 a month |
| Amazon SES | `email-smtp.<region>.amazonaws.com` · 587 | SES SMTP credentials (not IAM access keys) | sandbox: verified recipients only until you request production access |
| Gmail | `smtp.gmail.com` · 587 | your address · an app password (needs 2-Step Verification) | about 500 a day; sends as your Gmail address |
| Mailpit (dev) | `mailpit` · 1025 | none | local only, nothing is delivered |

Free tiers suit invitations and alerts for a team. They do not suit newsletters: daily caps
are low, a provider's branding or footer may be added, and a burst can pause the account.

## Domain authentication

Mail providers deliver mail only when the receiving server can check that your domain allowed
it. Set all three records in your domain's DNS; your provider shows the exact values.

- **SPF** (TXT on `example.com`): lists the servers allowed to send for the domain, for example
  `v=spf1 include:<provider's domain> ~all`. Keep one SPF record per domain and merge the
  includes.
- **DKIM** (TXT or CNAME at a selector, for example `mail._domainkey.example.com`): the provider
  signs every message, and the receiver checks the signature against this key.
- **DMARC** (TXT on `_dmarc.example.com`): tells receivers what to do when SPF or DKIM fails,
  for example `v=DMARC1; p=none; rua=mailto:dmarc@example.com`. Start with `p=none`, then move
  to `quarantine` once reports look clean.

**Why the sender must be your own domain:** Gmail, Outlook and Yahoo reject or spam-folder mail
whose From domain does not pass SPF or DKIM with DMARC alignment. A From address on a domain you
do not control, such as a free-mail address sent through another provider, fails that check.
BOSS therefore never sends as a shared BOSS address: each install sends as its operator.

## Environment instead of the console

An install can be configured with environment variables instead: `BOSS_EMAIL_SMTP_HOST`,
`BOSS_EMAIL_SMTP_PORT`, `BOSS_EMAIL_SMTP_USERNAME`, `BOSS_EMAIL_SMTP_PASSWORD`,
`BOSS_EMAIL_SMTP_USE_TLS`, `BOSS_EMAIL_FROM_ADDRESS`, and the switch
`BOSS_EMAIL_REAL_SEND_ALLOWED=true`. The bundled compose does not pass these through, so add them
to the `api` and `worker` environment in a compose override. Settings saved in the console take
precedence. Saving them counts as permission to send.

The dev inbox is `BOSS_EMAIL_DEV_INBOX=mailpit:1025`, and its UI is at
`BOSS_EMAIL_DEV_INBOX_URL`. The installer sets both when Mission Control is on. Set
`BOSS_MAILPIT_PORT` to move the UI off 8025. To run Mailpit on a headless install, use
`docker compose --profile mail up -d mailpit`.

## What BOSS never does

- It never prints or logs any part of the SMTP key. A failure reason has the key removed, and
  events record only *sent / failed / not configured* and the reason.
- It never loads remote images in its emails. The BOSS mark is attached to the message.
- It never emails a link without a public URL (**Setup › Public URL**). Until you set one, the
  invite dialog shows the link to copy.
