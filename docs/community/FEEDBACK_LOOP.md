# How a report becomes a change

A GitHub issue is the public record for a bug, a setup problem, or a feature request. When this repository is public, use the bug, installation, or feature form under `.github/ISSUE_TEMPLATE/`. The repository has not been published yet.

If you think you found a vulnerability, do not open a public issue. Email **boss@sean.net**. [SECURITY.md](https://github.com/seanventures/boss/blob/main/SECURITY.md) says what to include. Do not put credentials in that email.

You do not need an internal ticket id. Write what you were doing, what you expected, and what happened.

## The path

1. **You report it.** Include the version or commit, the install method, and the edition. Remove credentials and private data. The issue stays open while someone looks at it.
2. **Someone reproduces it.** Maintainers try the steps, ask for a missing detail, or link a duplicate. Bug and installation reports wait for a person. A complete feature request may get a draft plan. That plan cannot start work, spend money, or change a repository.
3. **A person decides.** The issue gets an outcome: accepted, with the scope; needs more information; duplicate; deferred; or declined, with a reason.
4. **An accepted change is a pull request.** The request names the tests and any budget, receipt, or database change. A bot may comment. It cannot merge.
5. **The issue stays open until the result is linked.** Before closing it, a maintainer links the change that shipped and says how to check it. If that does not match what you reported, say so or file a regression.

The signed GitHub App is optional. If a maintainer turns it on, BOSS can post one acknowledgement on the issue and record a decision that waits for a person. That reply is not a fix and not a promise to ship. The maintainer has to configure a signed webhook and review the App's permissions first. Without the App, a person does the same steps.

Opening an issue does not authorize those steps. This loop does not collect usage data from your self-hosted install. You choose what you put in the issue.

## What this does not do

- Opening an issue does not spend money, run code, write to a provider, deploy, or merge.
- An acknowledgement is not a resolution.
- A draft plan is not permission to start work.

## Next

- [Contributing](https://github.com/seanventures/boss/blob/main/CONTRIBUTING.md) if you want to propose the change yourself.
- [Quickstart](QUICKSTART.md) if you are still trying to get the console up.
- [Guide index](README.md) for the rest of the docs.
