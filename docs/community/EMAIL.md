# Email

BOSS sends invitations, password resets, sign-in codes and workflow mail. BOSS is open source:
it ships the email templates and an SMTP sender, and nothing else. **Each install brings its own
mail service and its own domain**: Brevo, Amazon SES, Postal, a company relay, or any SMTP
server. BOSS ships no sender address, no domain and no provider key.

You connect your mail service once, as the **Email (SMTP)** connector.

## Connect Email (SMTP)

In the console, open **Skills & MCP** and choose **Email (SMTP)**. A start checklist that needs
mail links to the same place (*Outgoing email › Open setup*).

1. Paste one **URL** for your mail server (examples below), or open **Fields** and fill in host,
   port, TLS, login and key separately. Both store the same settings.
2. Enter a **From** address on your own domain, for example `boss@example.com`, and optionally a
   **From name**, for example `Example Ops`.
3. Press **Save & test**. BOSS sends you a test message and saves the settings only if the server
   accepted it.

When the connector is connected, it shows a status pill, the address it sends as and the last
test. **Send test** sends another test message and shows the server's own reply, for example
`250 2.0.0 Ok: queued as 4ZxY`, or the refusal, for example `535 5.7.8 authentication failed`.
**Edit** changes the settings. **Disconnect** removes them.

The key or password goes to BOSS's secret store. BOSS shows it only as **Set**, with an **Edit**
button. BOSS never shows the key again, never logs it, and never puts it in an event.

### The connection URL

```
smtp://LOGIN:KEY@HOST:587     STARTTLS (the usual setting; 587 if no port is given)
smtps://LOGIN:KEY@HOST:465    TLS from the first byte (465 if no port is given)
```

**Percent-encode** any special character in the login or the key: `@` is `%40`, `/` is `%2F`,
`:` is `%3A`, `#` is `%23`, `?` is `%3F`, `%` is `%25`, `+` is `%2B`, a space is `%20`. If the
URL is wrong, BOSS says what is wrong (the scheme, a missing host, a bad port, an unencoded
character) and never repeats the URL back.

A server that takes no login, such as a company relay that trusts your network, needs only the
host: `smtp://relay.example.com:25`. If it offers no TLS at all, open **Fields** and set TLS to
**None**.

| Service | URL (replace the capitals) | Notes |
|---|---|---|
| Brevo | `smtp://LOGIN%40smtp-brevo.com:SMTPKEY@smtp-relay.brevo.com:587` | Login and SMTP key are under **SMTP & API › SMTP**. The login contains `@`, so write `%40`. |
| Amazon SES | `smtps://SMTPUSER:SMTPPASSWORD@email-smtp.REGION.amazonaws.com:465` | Use the SES **SMTP credentials**, not IAM access keys. The password often has `/` or `+`: write `%2F` and `%2B`. |
| Postal | `smtp://boss:CREDENTIALKEY@postal.example.com:25` | Create an **SMTP** credential on your Postal mail server. Postal checks only the key, so any login name works (see your Postal version's docs). |
| Gmail (app password) | `smtp://you%40example.com:APPPASSWORD@smtp.gmail.com:587` | Needs 2-Step Verification and an app password. Leave out the spaces Google shows in the app password. Mail is sent as your Google address. |
| Company relay | `smtp://relay.example.com:25` or `smtp://203.0.113.25:25` | Ask your mail admin which host, port and TLS setting to use. |

### Example: Brevo, step by step

1. In Brevo, open **Senders, Domains & Dedicated IPs › Domains**, add `example.com`, and add the
   DNS records it lists (a Brevo code, DKIM and DMARC). Wait until it shows *Authenticated*.
2. Under **Senders**, add `boss@example.com`.
3. Open **SMTP & API › SMTP**. Copy the **Login**, then press **Generate a new SMTP key** and copy
   the key. Brevo shows the key only once.
4. In BOSS, open **Skills & MCP › Email (SMTP)**. In URL, enter
   `smtp://LOGIN:KEY@smtp-relay.brevo.com:587`, with the `@` in the login written as `%40`. In
   From, enter `boss@example.com`. Press **Save & test**.
5. Invite someone under **People › Invite people**. The dialog shows *Sent*, or *Failed* with the
   reason, and offers **Resend** and **Copy link**.

Free tiers (Brevo, Resend, Mailjet and others) suit invitations and alerts for a team. They do
not suit newsletters: the daily caps are low, the provider may add its branding, and a burst of
mail can pause the account. Check each provider's current terms.

## Send from your own domain (SPF, DKIM, DMARC)

Receiving servers deliver your mail only when they can check that your domain allowed it. Add
all three records in your domain's DNS. Your mail service shows the exact values.

- **SPF** (a TXT record on `example.com`) lists the servers that may send for the domain, for
  example `v=spf1 include:<your service's domain> ~all`. Keep one SPF record per domain, and
  merge the includes into it.
- **DKIM** (a TXT or CNAME record at a selector, for example `mail._domainkey.example.com`)
  holds the key your service signs each message with. The receiver checks the signature.
- **DMARC** (a TXT record on `_dmarc.example.com`) tells receivers what to do when SPF or DKIM
  fails, for example `v=DMARC1; p=none; rua=mailto:dmarc@example.com`. Start with `p=none`, and
  move to `quarantine` when the reports look clean.

**Why the From address must be on your own domain:** Gmail, Outlook and Yahoo reject mail, or
send it to spam, when the From domain does not pass SPF or DKIM with DMARC alignment. A From
address on a domain you do not control fails that check. For this reason BOSS never sends from a
shared BOSS address: each install sends as its operator.

## Which route mail takes

BOSS picks one route for every email, in this order:

| Route | When | Mail leaves the machine |
|---|---|---|
| **Email (SMTP) connector** | You connected it in Skills & MCP | Yes |
| **Environment** | `BOSS_EMAIL_SMTP_*` is set and `BOSS_EMAIL_REAL_SEND_ALLOWED=true` | Yes |
| **Local test inbox (Mailpit)** | You installed with `--with-mailpit` | No |
| **None** | None of the above | No. Invites show a link to copy, and BOSS says why. |

**Upgrading:** mail settings saved in the console before this version (the old Email form) show
up as the Email (SMTP) connector after the upgrade. Nothing needs re-entering. The stored key
now shows as **Set** only.

## A local test inbox (Mailpit), on request

Mailpit catches every email BOSS sends, so you can read invitations and alerts without any
mail account. Nothing is delivered. It is **off by default**. To turn it on, install with:

```
./install.sh --with-mailpit
```

or set `BOSS_EMAIL_DEV_INBOX=1` before you run the installer. A rerun keeps your choice, and
`--no-mailpit` turns the inbox off again. The inbox UI is at `http://localhost:8025`. Set
`BOSS_MAILPIT_PORT` to use another port.

With Mailpit on, the connector offers **Local test inbox** (labelled *Local only*). Choose it,
then press **Use local test inbox**. An invitation that lands in Mailpit does not verify the
person's address, because anyone on the machine can read the inbox.

## The environment instead of the connector

An install can set mail with environment variables instead: `BOSS_EMAIL_SMTP_HOST`,
`BOSS_EMAIL_SMTP_PORT`, `BOSS_EMAIL_SMTP_USERNAME`, `BOSS_EMAIL_SMTP_PASSWORD`,
`BOSS_EMAIL_SMTP_USE_TLS`, `BOSS_EMAIL_FROM_ADDRESS`, and the switch
`BOSS_EMAIL_REAL_SEND_ALLOWED=true`. The bundled compose file does not pass these through, so
add them to the `api` and `worker` environment in a compose override. The connector takes
precedence. Connecting it counts as permission to send.

## What BOSS never does

- It never shows, logs or emits any part of the SMTP key. A failure reason has the key removed,
  and events record only *sent*, *failed* or *not configured*, with the reason.
- It never loads remote images in its emails. The BOSS mark is attached to each message.
- It never emails a link without a public URL (**Setup › Public URL**). Until you set one, the
  invite dialog shows the link to copy.
