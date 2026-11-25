# Connect GitHub

BOSS needs to reach your repos so it can push branches and open pull requests for you to review. You connect GitHub once. It takes about a minute, and there is no token to make, copy or renew.

## The steps

1. In BOSS, open **Setup**. Under **Repos**, choose where your repos are:
   - **My GitHub account**: repos under your own name.
   - **An organisation**: type its name, as it appears in `github.com/<name>`. You need to be an owner of the organisation. If you aren't, ask an owner to do this step.
2. Click **Connect GitHub**. GitHub opens with BOSS's app already filled in. Click **Create GitHub App**.
3. GitHub asks where to install it. Pick **Only select repositories**, tick the repos BOSS may work on, and click **Install**.
4. You're back in BOSS, and your repos are listed. Pick one and click **Connect**. BOSS checks that it can **read**, **write** and **open pull requests**, and shows three ticks.

That's it. Your next step in Setup is choosing a model.

## Good to know

- **Access never runs out.** The app belongs to your install, and its key does not expire. Before every GitHub call, BOSS asks GitHub for fresh access that lasts an hour, so a build of any length never stops because access expired.
- **BOSS only touches the repos you picked.** It never pushes to your default branch. Everything comes to you as a pull request.
- **Add or remove repos later** with **Add repos on GitHub** in Setup, or on GitHub under **Settings › Applications › Installed GitHub Apps**. Then come back to Setup and the list updates.
- **The app is public on purpose — your repos are not.** GitHub installs a *private* app only on the account that created it and offers no way to change that later by API, which would wall every other account (a second personal account, another organisation) out with a bare 404. So BOSS creates the app public. Public only means other accounts *may* install it: each installation grants access to exactly the repos that installation picked, nothing is shared, and nobody can see or touch your repos through it.
- **Your install only needs to be reachable from your own browser.** GitHub sends you back through your browser, so this works on a laptop, a tailnet or a private network.

## If something goes wrong

| What you see | What to do |
|---|---|
| "Type the organisation's name" | Choose **My GitHub account**, or type the organisation's name. |
| GitHub says you can't create apps for the organisation | You aren't an owner. Ask an owner to click **Connect GitHub**, or use **My GitHub account**. |
| "The link expired or was used. Start again." | The GitHub step took more than 10 minutes. Click **Connect GitHub** again. |
| Back in BOSS, but no repos are listed | You installed the app without picking repos. Click **Add repos on GitHub**, tick them, and come back. |
| "Set BOSS_PUBLIC_URL first" | BOSS doesn't know its own address yet. Claim the install from its address first (see [Getting started](GETTING_STARTED.md)), then try again. |
| GitHub shows a bare 404 on the app's install page | Two known causes. (1) Your app was created before BOSS made apps public: a private app installs only on its creator. Owner: `github.com/settings/apps/<slug>` → **Advanced** → **Make public** — or skip the app route and connect the account with a sign-in or token (the Add-account sheet offers this itself). (2) A personal account's install page only opens signed in as that very person: switch accounts on GitHub (avatar menu) and try again. |
| Adding a second account lands on a 404 | Same as above — the Add a GitHub account sheet detects both cases and leads with a route that works. |

## Other ways

Open **Other ways** under Connect GitHub only if the app route can't work for you:

- **GitHub Enterprise Server without apps, an offline install, or an organisation that blocks apps:** paste a fine-grained access token. **Create token** opens GitHub with the two permissions BOSS needs ticked. A token expires on the date you choose, and builds stop when it does, so pick the longest expiry your organisation allows. Setup shows the expiry date ahead of time.
- **GitHub Enterprise Server:** set the install's `BOSS_GITHUB_API_URL` / `BOSS_GITHUB_WEB_URL` ([GitHub Enterprise](IDENTITY_ADMIN.md#github-enterprise)), or give the connection its own `api_base_url` (`https://<your-host>/api/v3`). Every GitHub call for that connection's repositories then goes to your server: clone, pull requests, checks, events. Custom CA, proxy and what is proven so far are in [Enterprise network › GitHub Enterprise Server](ENTERPRISE_NETWORK.md#github-enterprise-server).
- **Already connected with a token?** Nothing changes. Your install keeps using it.

## Events from a repository GitHub cannot reach

An event route (a pull request opened, a branch pushed) normally gets GitHub's webhook at
`/v1/intake/github/<token>`. An install on a laptop or behind a firewall has no address GitHub
can reach. For those, create the GitHub intake connection in **poll** mode:

```json
{"kind": "github", "mode": "poll", "name": "tenants",
 "config": {"repository": "owner/name", "poll_seconds": 30},
 "secrets": {"api_token": "<optional; else the project's GitHub connection is used>"}}
```

- One poller per connection, on any number of workers: it reads the repository's open pull
  requests and branch heads every `poll_seconds` (default 30, at least 15) with conditional
  requests, so an unchanged repository costs nothing against GitHub's rate limit.
- A new or moved pull request head becomes `com.github.pull_request.opened` /
  `.synchronize`, and a moved branch becomes `com.github.push`: the same envelopes the
  webhook gives, so the same routes and filters match. The first poll records what is already
  there and only reports pull requests updated after the connection was made.
- A poll connection still accepts the webhook if you add one (give it a `webhook_secret`). A
  commit that arrives both ways is one event.
- A refused token or a rate limit backs the poller off (at most 15 minutes) and shows on the
  connection's health. The token never reaches an agent.

To route only pull requests that touch some files, filter on their changed files (BOSS fetches
the list only when a route asks for it):
`{"field": "data.changed_files", "op": "glob", "value": "tenants/*.yaml"}` (`*` stays in one
folder, `**` spans folders).

`gh webhook forward --repo owner/name --events pull_request --url
http://127.0.0.1:8000/v1/intake/github/<token> --secret <webhook secret>` (a GitHub CLI
extension) forwards real deliveries to a laptop. Use it only to record a demo: it is in beta,
one forwarder per repository, needs repository admin, and stops when the terminal closes. Poll
mode is the supported path.
