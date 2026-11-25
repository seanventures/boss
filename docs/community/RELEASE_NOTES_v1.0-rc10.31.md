# BOSS Community 1.0.0-rc.10.31 candidate

RC10.31 makes each project explain itself. The project page now says what the app is, what was discussed and what was built, and the Work row names the real next step.

This is a test candidate. Cutting the tag does not deploy a host or publish an image.

## What is better for you

**Projects explain themselves**

- Every project page has an **About this app** card: a short spec summary, which must-haves are met or partly met, and **Read the full spec**.
- Each piece of work shows **What was discussed** and **What was built**.

**The Work row says what happens next**

- The Work row's button names the real next step, for example **Merge change #3 · deploys to staging after**.
- Every change has **Open preview**, **Start preview** or **Recreate preview**, whichever applies.
- Changes get short titles taken from the spec, and the Outputs header matches Checks.

**Opening things works from anywhere**

- Build pages open from any project, and "Can't reach BOSS" shows only when BOSS is actually down.
- The sidebar keeps your current project, the newest one, and the last one you opened.
- Docs updates never ask for GitHub on projects kept on this machine.

**Chat windows**

- Chat windows have an **Attach** icon and a full-screen button on every chat.
- Helm replies are short, with the details folded away.

## Known issues

- When a model fails because of your setup (for example a plain `http://` address), Easy Mode's panel shows one clear card plus a second, vaguer "Background job · Panel step" card; use the clear one. Retrying both can run the panel twice.
- A model added under **Local › Other URL** (LM Studio, vLLM) shows Working but cannot be used yet. Use Ollama, or an `https://` address under API key › Other.

- **Command Center may still say "Connect a repository"** when your code is kept on this machine. Setup shows Repos as **On this machine**, and builds work; you can ignore that card.
- **A model at a plain `http://` address** (LM Studio, vLLM or LiteLLM on your network, added under **API key › Other**) passes its connection test, then builds refuse to send it the key. Use an `https://` address for now. OpenRouter, OpenAI, Anthropic, Gemini and xAI are not affected.
- Reloading the recovery-codes screen loses the codes it showed; create new ones from **My account** if that happens.
- A build waiting for a model is listed under **Running**; its card says **Waiting for a model**.
- **A model added under Local › Other URL** (LM Studio, vLLM) connects, then builds fail with "VLLM_BASE_URL is not configured". Use Ollama, or an `https://` address under API key › Other, for now.
- On the project page, the "Use an https address" card can squeeze its text into a narrow column.
- For repositories hosted on GitHub, merge still takes the pull request tip; the follow-up-after-merge fix covers repositories kept on this machine.
- Easy Mode never auto-merges; you press **Approve and merge**.

## Not in this tag

- Opening a pull request on GitHub still needs a GitHub account and the BOSS GitHub App; a model still needs a subscription sign-in, an API key, or a local model.
- The release still has no published container images; the installer builds them on your machine (about 10 minutes on a fast host).

## Checks

- The version pins match `1.0.0-rc.10.31` (`engine/tests/test_distribution_contract.py`).
- Verified on a local install of this commit (sign-in, project page, previews, work row). The clean-install stranger test runs next.
