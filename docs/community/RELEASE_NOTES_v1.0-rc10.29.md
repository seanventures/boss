# BOSS Community 1.0.0-rc.10.29 candidate

RC10.29 is about the first project working end to end. The gallery only offers what your install can build, Java and Go come built in, and each template shows a drawing of the app it makes. A normal build now ends as **ready** with any review notes queued as follow-ups that BOSS fixes itself, BOSS retries its own failures, and when something needs you the card says what to do in plain words.

This is a test candidate. Cutting the tag does not deploy a host or publish an image.

## What is better for you

**The template gallery**

- The gallery only offers templates BOSS can build. The three Android templates (Pantry list, Shift clock, Trail log) are gone, because no BOSS image has an Android SDK, an emulator or Xcode. **Orbit dodge**, which was a Godot game, is now a browser game with the same gameplay.
- Every template knows what it builds with (Python, Node, Go or Java), so BOSS can refuse one this install cannot build, in words, before anything starts.
- If a template needs a toolchain your install does not have, its card says **Needs Go** (or **Needs Java**) and shows the command to turn it on, for example `boss toolchains enable go`. You cannot pick it by mistake, and if something asks BOSS to start one anyway, BOSS says in words what is missing before it makes a project.
- Each card opens with a drawing of that app's screen, like the "Start from an idea" cards.
- The gallery shows two rows first, with **Show all** to see every template and **Show fewer** to fold it back.
- The "Build an Android app" suggestion is now "Build a web app", and a phone preview frame that promised signed mobile builds is gone. BOSS does not build Android or iOS apps.
- Templates that needed a download at build time (a similarity model file, native sqlite modules) now use what is already on the machine.

**Java and Go are on by default**

- A new install builds Java and Go into BOSS's image, next to Python and Node. The image is about 0.75 GB larger.
- An existing install gets them on its next `boss update` or `boss upgrade`, unless its `.env` says `BOSS_TOOLCHAINS=none` or lists its own toolchains; those choices are kept.
- `boss toolchains` says when it is showing the default. `boss toolchains disable go` still turns Go off and remembers it.
- When the toolchains change, the update also rebuilds the egress (and runner) service, so Maven Central, the Gradle portal and the Go proxy are reachable when the image has those toolchains.

**Remote installs clean up after themselves**

- `remote.sh install` and `remote.sh update` now mark the server as a dedicated BOSS host (`BOSS_DEDICATED_HOST=1`) when that line is missing, so old Docker build cache is cleaned automatically. A value you set yourself, `0` included, is never changed. Laptop and local installs are not affected.

**A clearer start screen**

- The **Build with BOSS** box has an icon on every control (Add, Mode, Mic), coloured suggestion chips, and an **Easy Mode** button that stands out and stays readable.
- The menus in the top bar (**Builds**, the bell, your account) open at their own width. Before, **Find a build** was squeezed to the width of its button, so its search box, tabs and build names were cut off.

**Builds finish instead of stopping to ask you**

- **A light lane for normal builds.** Easy Mode, templates, samples and New work run at the *standard* quality bar. A build stops only for real breakage: it does not build, the app does not start, its own tests fail, a security or secrets problem, or tests weakened to pass. Review comments, missing must-haves and process notes become follow-ups: the card reads "*App* is ready — N follow-ups queued, BOSS is fixing them" and merges with a normal **Approve and merge**. Production deploys stay strict and re-check anything judged at the lighter bar.
- **One fix round, lean requirements.** A normal build gets at most one review fix round, then hands over. Requirements are a few behaviour checks, not rules about test names or PR wording. A *Bug* request takes the small path (one build, no panel).
- **BOSS retries its own failures.** A failure on BOSS's side is retried automatically, at most twice, spaced out, from the saved work. Builds a BOSS bug stopped in the last 72 hours restart once after you update. A model usage limit waits for the reset and carries on.
- **Your setup, said plainly.** A failure only you can fix (a plain-`http` model address, a rejected key, a repository BOSS cannot reach) is never retried in a loop; the card says what to do, for example "Use an https address (Setup › Models)".
- **Failed builds are visible.** The project page, Command Center, bell and Builds menu say when the latest build failed, why, and offer the fix or **Retry**. Older attempts a later build replaced read as history, not red.
- **Plain words.** Raw codes (gate keys, `consent_required`, "retry N", internal URLs) no longer appear; a test keeps them out.

**Easy Mode**

- **Confirm it's you** opens when your sign-in code is stale, instead of "The build did not start." The same fix covers 23 other buttons.
- After a start, a card says "*App* is building" with **Watch the build**; pressing Easy Mode again on a running build says it already started, and a finished or failed one starts fresh.

**Previews**

- Next.js previews no longer open as garbled text in the browser, and oversized responses are refused safely.
- App data (for example recipes you add) is kept across preview restarts; **Fresh data** starts empty.
- The **App ↗** link opens the preview, and a working Next.js page is no longer mistaken for an error page.

**Security**

- BOSS's own git work on a build's code runs as the build's user, never as BOSS, and writes inside a build cannot be redirected outside it (found and fixed during this release's reviews).

## Known issues

- **Command Center may still say "Connect a repository"** when your code is kept on this machine. Setup shows Repos as **On this machine**, and builds work; you can ignore that card.
- **A model at a plain `http://` address** (LM Studio, vLLM or LiteLLM on your network, added under **API key › Other**) passes its connection test, then builds refuse to send it the key. Use an `https://` address for now. OpenRouter, OpenAI, Anthropic, Gemini and xAI are not affected.
- Reloading the recovery-codes screen loses the codes it showed; create new ones from **My account** if that happens.
- A build waiting for a model is listed under **Running**; its card says **Waiting for a model**.
- **A model added under Local › Other URL** (LM Studio, vLLM) connects, then builds fail with "VLLM_BASE_URL is not configured". Use Ollama, or an `https://` address under API key › Other, for now.
- On the project page, the "Use an https address" card can squeeze its text into a narrow column.
- A follow-up that runs after you merged its parent may not open its own change yet; we are checking where its fixes land.
- The first update after this release downloads the JDK, Maven, Gradle and Go (about 0.75 GB), so it takes longer than usual.

## Not in this tag

- Opening a pull request on GitHub still needs a GitHub account and the BOSS GitHub App; a model still needs a subscription sign-in, an API key, or a local model.
- The release still has no published container images; the installer builds them on your machine (about 10 minutes on a fast host).

## Checks

- The version pins match `1.0.0-rc.10.29` (`engine/tests/test_distribution_contract.py`).
- Every change in this tag was reviewed by an independent agent before merge, with its own tests, and passed the merge gate.
- Stranger test: a clean install of this exact commit on a separate host, following README and QUICKSTART, passed (round 7: SHIP). A fresh Easy Mode project on the release build went template → build → **ready, 4 follow-ups queued** → merged → app opened, with no person needed beyond consent and the merge.
