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
- **The app is private to you.** It is created on your account or organisation, not shared with anyone else.
- **Your install only needs to be reachable from your own browser.** GitHub sends you back through your browser, so this works on a laptop, a tailnet or a private network.

## If something goes wrong

| What you see | What to do |
|---|---|
| "Type the organisation's name" | Choose **My GitHub account**, or type the organisation's name. |
| GitHub says you can't create apps for the organisation | You aren't an owner. Ask an owner to click **Connect GitHub**, or use **My GitHub account**. |
| "The link expired or was used. Start again." | The GitHub step took more than 10 minutes. Click **Connect GitHub** again. |
| Back in BOSS, but no repos are listed | You installed the app without picking repos. Click **Add repos on GitHub**, tick them, and come back. |
| "Set BOSS_PUBLIC_URL first" | BOSS doesn't know its own address yet. Claim the install from its address first (see [Getting started](GETTING_STARTED.md)), then try again. |

## Other ways

Open **Other ways** under Connect GitHub only if the app route can't work for you:

- **GitHub Enterprise Server without apps, an offline install, or an organisation that blocks apps:** paste a fine-grained access token. **Create token** opens GitHub with the two permissions BOSS needs ticked. A token expires on the date you choose, and builds stop when it does, so pick the longest expiry your organisation allows. Setup shows the expiry date ahead of time.
- **Already connected with a token?** Nothing changes. Your install keeps using it.
