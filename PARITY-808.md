# BOSSAPP-808 parity: old control → where it lives now

The Repo and models area (rail entry unchanged) now opens on **Welcome** until GitHub, a model and a
first PR are done, then on **Setup** (three blocks). The whole earlier board is kept, row for row,
under **Setup › Advanced settings** (a fold, rendered in the DOM even when closed, so every
`data-testid` and deep link still resolves). `SetupBoard` without `layout` still renders the classic
board, which every existing test mounts.

| Old control | Where it lives now | Note |
|---|---|---|
| Header verdict pill | Setup header (verdict) + Advanced settings header | same server pill |
| Test all | Advanced settings header | unchanged |
| Next: open a PR / Next: add skills | Advanced settings header; Welcome step 3 | unchanged |
| ① GitHub — Sign in with GitHub (device flow), Switch account | Add GitHub account › My own account; Advanced settings › GitHub row | same DeviceSignin |
| Repos row: paste token, Create token link, Other ways, consent | Add GitHub account › Paste an access token; Advanced settings › Repos | same PasteTokenForm |
| Repos row: Use the GitHub App (repo_app) | Add GitHub account › Paste an access token form (button when the App is installed); Advanced settings › Repos | unchanged |
| Repos row: pick repos after sign-in (repo_signin), Install BOSS on <org>, Check again | Add GitHub account › step 2 (checkbox list + install links) | same API ops |
| Repo checks Read / Write / Pull requests | Setup › Repositories (Access column); Add GitHub account › step 3; Advanced settings › Repos | same server checks |
| Replace token, Fix on GitHub, Test, Make default, Remove (row ⋯) | Setup › GitHub accounts › Fix (Replace token); Advanced settings › Repos (all) | unchanged |
| + Add repo (same connection) | Setup › Repositories › + Add repo (pick account, same AddRepo); Advanced settings › Repos | same repo_select |
| Org GitHub App from manifest, installation picker, verify, disconnect | Advanced settings › Advanced › GitHub App; linked from accounts Fix (App) and Add GitHub account › An organisation › Use your own GitHub App | unchanged |
| Fine-grained org token in vault, env BOSS_GITHUB_TOKEN migration | Advanced settings › Advanced › GitHub App row (unchanged); env-token install shows as a "Server settings" account | unchanged |
| Repository registry (org) | Advanced settings › Advanced › Repositories; allowed repos also listed in Setup › Repositories | unchanged |
| Per-project repos (multirepo/project-repos) | project page, unchanged; Setup › Repositories shows "Used by" | unchanged |
| CI permission toggle | Advanced settings › Advanced › CI permission | unchanged |
| Models row: + Add a model (Subscription Claude/Codex/Grok sign-in, API keys, Local), proof call | Setup › Models › + Add model (same AddModelPanel); Welcome step 2; Advanced settings › Models | unchanged |
| Model sources (capacity), Replace key, Sign in again, Default model, More sources | Advanced settings › Models | unchanged |
| Held proofs (Release the hold) | Advanced settings › Models | unchanged |
| Embedding model | Advanced settings › Models | unchanged |
| Model routing Smart / Fixed, pins (Who does what) | Setup › Models › Who does what › Change → Advanced settings › routing | unchanged |
| Budget row | Advanced settings › Advanced › Budget | unchanged |
| Build database, Build speed, Toolchains, Public URL | Advanced settings › Advanced | unchanged |
| Try it (open your first PR) | Advanced settings › Try it; Welcome step 3 links to the sample (`/?new_project=sample`) | the sample flow is BOSSAPP-810 |
| Improve BOSS sharing ask | bottom of the page, both screens | unchanged |
| Deep links `?area=setup&focus=…&add=…&source=…`, legacy `step=` | open Setup with Advanced settings open on that row | Welcome never shown on a deep link |

## API and data

* `GET /v1/setup`: every field kept; **added** `github_accounts[]` and `repositories[]`.
* `store_org_token(keep_others=False)` (Identity › GitHub paste, env migration) still replaces the
  org token. Setup's paste now keeps another account's token (`keep_others=True`); only an older
  token of the same login is retired, its repos carried over.
* Token selection: `control_plane_token`, egress `_credential_for_run` and the registry prober use the
  org token that covers the repo (`org_token_for`); with one token this is the old behaviour.
* No schema migration. An install with one connection shows it as the first account row.
