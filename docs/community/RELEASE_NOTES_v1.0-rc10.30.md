# BOSS Community 1.0.0-rc.10.30 candidate

RC10.30 is about Easy Mode working on its own. It runs a quick spec panel, then build, P1 tests, preview and a deploy to staging. Every failure has an agent that owns it, failed deploys say what BOSS is doing about them, and every project page shows your app.

This is a test candidate. Cutting the tag does not deploy a host or publish an image.

## What is better for you

**Easy Mode**

- Easy Mode runs a quick spec panel first, then build, P1 tests, preview, and deploy to staging. When there is no server to deploy to, the deploy step is skipped and says so in plain words.
- Every Easy Mode build always gets its delivery workflow, under the one consent you give at the start.
- Easy Mode never auto-merges. You press **Approve and merge**.
- Starting Easy Mode no longer holds up sign-in while the spec panel launches.

**Merging**

- Merge takes the approved head. A follow-up that runs after the merge opens its own change.

**Failures have an owner**

- Every failure in BOSS has an agent that owns it. Either the agent is fixing it, or you get one action item with the reason and **Done - retry**.
- A failed deploy says what BOSS is doing about it, and an app that crashes after a deploy is caught in about 30 seconds.

**Your app on every project page**

- Every project page shows **Your app** with **Open app**.
- Previews are kept running while memory allows. **Recreate preview** rebuilds one, and a sleeping link wakes the preview when you open it.

## Known issues

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

- The version pins match `1.0.0-rc.10.30` (`engine/tests/test_distribution_contract.py`).
- Stranger test: pending
