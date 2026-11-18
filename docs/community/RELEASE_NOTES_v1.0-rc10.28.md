# BOSS Community 1.0.0-rc.10.28 candidate

RC10.28 is about the first hour. Friends of the project installed rc10.27 from the README, word for word, and we fixed everything that got in their way. If you tried rc10.27 and stopped, this is the one to try.

This is a test candidate. Cutting the tag does not deploy a host or publish an image.

## What is better for you

**Installing and claiming**

- The installer's last screen starts with the claim: run `boss identity setup-link`, open the link, create the Owner. `boss open` and `boss doctor` say the same thing.
- Pasting the claim link into a tab that is already on the claim page now works. The link leaves the address bar once you have claimed.
- A new install no longer asks "Which seat were you?".
- The optional local model (about 4.4 GB) is no longer installed when you just press Enter.
- `boss doctor` says "BOSS is running, N warnings to look at" instead of "ready" when something needs a look, and its SSH tunnel line includes the preview port.
- A new install that is claimed with an Owner account no longer also accepts the shared console password (which had no second factor). Upgrades keep it for their window; `boss identity legacy restore --path shared_password` brings it back on purpose.
- Sign-in only shows the methods that are set up (no email tabs without email, no disabled provider tiles).
- `boss stop` and `boss start` stop and start this install, every service included.
- Two installs on one machine are documented and no longer step on each other: each saves its own project name, and each `boss` command runs its own install (`boss --dir PATH` for another one).

**The first screens**

- Command Center's template gallery loads again. It said "Templates did not load." on every install.
- **Easy Mode** works: pick a template or describe an app, press Easy Mode, allow the agents to act for you, and BOSS makes the project, gives it a repository on this machine, builds it and opens its pull request for you to review.
- **Try a sample** lists everything it needs up front, gets its own repository on this machine, and does not need a deployment server to build and open its pull request.
- Code can stay on the install machine: a project with a local repository counts as connected; GitHub is optional.
- Buttons that did nothing now go where they say (Add model, Add a repo, First PR), and counts agree (projects, approvals, workflow steps).
- The project's progress strip no longer prints overlapping text.
- Internal-only menu items are hidden on a new install, and the model picker lists only sources that work in this edition.
- Regenerating recovery codes asks you to confirm and to verify it is you.

**Fixed for everyone**

- BOSS's host tools can see a server your organisation shares with the project (they said "no registered host").
- Confluence lives under **Setup › Documents**, not Ticket systems.
- A build that finishes without a pull request now says why.

## Known issues

- **Command Center may still say "Connect a repository"** when your code is kept on this machine. Setup shows Repos as **On this machine**, and builds work; you can ignore that card.
- **A model at a plain `http://` address** (LM Studio, vLLM or LiteLLM on your network, added under **API key › Other**) passes its connection test, then builds refuse to send it the key. Use an `https://` address for now. OpenRouter, OpenAI, Anthropic, Gemini and xAI are not affected.
- Reloading the recovery-codes screen loses the codes it showed; create new ones from **My account** if that happens.
- A build waiting for a model is listed under **Running**; its card says **Waiting for a model**.

## Not in this tag

- Opening a pull request on GitHub still needs a GitHub account and the BOSS GitHub App; a model still needs a subscription sign-in, an API key, or a local model.
- Production deploys do not yet use the new quality-bar presets (they are on the next release's list).
- The release still has no published container images; the installer builds them on your machine (about 10 minutes on a fast host).

## Checks

- The version pins match `1.0.0-rc.10.28` (`engine/tests/test_distribution_contract.py`).
- Every change in this tag was reviewed by an independent agent before merge, with its own tests, and passed the merge gate.
- A clean, side-by-side install of this tree (commit 13ce3e8f4) was walked by an agent following QUICKSTART on Ubuntu 24.04 x86_64, four rounds in all, until it found no blocker: install (about 6–10 minutes), claim with an authenticator app, Setup, template gallery, Easy Mode with and without a model, Try a sample, Start a new app on this machine, Approvals, the model screen, `boss stop` / `boss start`, the sign-in page and every link.
- On a second install with a model connected, a template build ran from Easy Mode through consent, implementation, review and tests to its pull request and merge card.
