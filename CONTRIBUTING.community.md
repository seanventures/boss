# Contributing to BOSS Community

You can propose a documentation change or a code change without access to a maintainer's machines. This repository has not been published yet. When it is, the work lands through a GitHub issue and a pull request.

## Report it first

Search existing issues before you open another one. For a bug, a setup problem, or a feature idea, follow the [feedback process](docs/community/FEEDBACK_LOOP.md). Say what you were doing, what you expected, how you installed, and which edition you are running. Remove secrets and customer data from logs and screenshots.

A vulnerability goes to [boss@sean.net](mailto:boss@sean.net). Do not open a public issue for a vulnerability. See [Security](SECURITY.md).

## Propose the change

1. Fork the repository when that is available, and use a focused branch. Until the repository is public, a maintainer can still take a patch that follows these rules.
2. Keep the pull request small. Explain what a user can do differently, which tests you ran, and any migration or compatibility change.
3. Add tests for state transitions you touch. If the change can spend money or write outside BOSS, test the budget reservation, the receipt, the worker lease, and the tenant boundary as well. Run the relevant tests and lint checks before you ask for review.
4. Update the user guide or the API contract when behavior changes. A new console action needs a matching API permission check and an audit event.

Spend and external writes need a reservation and a receipt. Reserve budget before the spend. Record the external effect as a receipt with a dedupe key, through the existing adapter boundary. Do not commit credentials.

Installing agent definitions or example workflows starts no work. Do not describe a change as if installing them launches a run.

Maintainers review scope, safety, tests, and documentation before merging. An issue acknowledgement or a draft plan does not authorize that work. The [Code of Conduct](CODE_OF_CONDUCT.md) applies in project spaces. Product behavior for someone installing BOSS is in [README.community.md](README.community.md), and the page list is the [guide index](docs/community/README.md).
