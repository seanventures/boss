# BOSS Community 1.0.0-rc.6.1

A hotfix on `v1.0-rc6`: the console is fast again, and the Infra **Deploy** button works. Nothing changes in how you sign in or what anyone may do.

## Install or upgrade

```sh
curl -fsSL https://boss.sean.network/install.sh | bash
```

Already installed: `boss upgrade`.

## The console is fast again

In rc6 every page asked the API all of its ~40 feeds on every load, and each one paid the new access check, so the Factory took 3.6 to 4.6 seconds on a busy install. On a seeded test install:

- **Fewer requests.** A page now asks only for what it shows: the Factory makes 17 API calls instead of 38, the Overview 12 instead of 34.
- **Cheaper access checks.** Each signed-in request spends 2 statements on identity instead of 4 (1 for an org-wide one).
- **Smaller pages.** The Factory's code-task rows went from 283 KB to 134 KB, and the Command Center's first paint from 1.16 MB to 642 KB.
- **Short reuse, only for a live session.** The console may reuse an answer for up to 5 seconds, and only after the same page load has checked with the API that your session is still live and your access unchanged. A revoked session, a disabled person or a changed role takes effect on the very next request, as in rc6.

## The Infra Deploy button works

In rc6, **Deploy** on an app target in Infra returned a server error on every press. It now plans the deploy, which then goes through approval as usual. Registering and bootstrapping a deployment target from setup had the same fault and are fixed too.

## Version

The engine's Python package is `1.0.0rc6.post1`, because Python package versions cannot be written `1.0.0-rc.6.1`. Every other part of BOSS reports `1.0.0-rc.6.1`.
