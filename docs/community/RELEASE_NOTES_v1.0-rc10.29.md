# BOSS Community 1.0.0-rc.10.29 candidate

RC10.29 is about the template gallery. People trying BOSS for the first time could pick templates that BOSS can never build, and the two Go templates only worked if you had turned Go on yourself. Now the gallery only offers what your install can build, Java and Go come built in, and each template shows a small drawing of the app it makes.

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

## Known issues

- **Command Center may still say "Connect a repository"** when your code is kept on this machine. Setup shows Repos as **On this machine**, and builds work; you can ignore that card.
- **A model at a plain `http://` address** (LM Studio, vLLM or LiteLLM on your network, added under **API key › Other**) passes its connection test, then builds refuse to send it the key. Use an `https://` address for now. OpenRouter, OpenAI, Anthropic, Gemini and xAI are not affected.
- Reloading the recovery-codes screen loses the codes it showed; create new ones from **My account** if that happens.
- A build waiting for a model is listed under **Running**; its card says **Waiting for a model**.
- The first update after this release downloads the JDK, Maven, Gradle and Go (about 0.75 GB), so it takes longer than usual.

## Not in this tag

- Opening a pull request on GitHub still needs a GitHub account and the BOSS GitHub App; a model still needs a subscription sign-in, an API key, or a local model.
- Production deploys do not yet use the new quality-bar presets (they are on the next release's list).
- The release still has no published container images; the installer builds them on your machine (about 10 minutes on a fast host).

## Checks

- The version pins match `1.0.0-rc.10.29` (`engine/tests/test_distribution_contract.py`).
- Every change in this tag was reviewed by an independent agent before merge, with its own tests, and passed the merge gate.
- Stranger test: pending.
