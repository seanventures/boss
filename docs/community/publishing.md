# Publishing the docs

The repository provides the BOSS product manual in `docs/community/`, built with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/). The marketing website at <https://boss.sean.network/> has its own source repository and deployment. It imports the product manual and installer bootstrap from BOSS. Pages outside the manual, such as the installer guide and examples, are linked on GitHub.

## Build locally

From the repository root, install `mkdocs` and `mkdocs-material`, then run:

```bash
mkdocs build -f mkdocs.community.yml --strict
```

Use `mkdocs serve -f mkdocs.community.yml` for a live preview while you edit.

## Build check

The `Product docs` workflow in `.github/workflows/pages.yml` verifies the manual in strict mode under `_site/docs/`. The hosted website publishes its imported copy through its separate deployment process. A source release or local manual build does not verify the content currently served there.

## Installer pin

The release-pinned GitHub command in the installer guide selects one release tag. The installer resolves and records its commit. Update the tag when a new release is cut; use the recorded full SHA when comparing an installed checkout with validation evidence.

## Changes pushed to `main`

RC8 is an incremental release retaining the complete published RC6.2 history. Reviewed product changes and public guides are added as focused commits, preserving contributor ancestry. Reconstructed changes name their reviewed preparation commit with a `Prepared-from:` trailer; this is not a claim that the whole private source was exported.

A future full private-source export must first import all public contributor changes, retaining authors and `Imported-from:` provenance, and pass the export guard. That historical synchronization is still pending. A wholesale export that replaces the public tree remains refused until the guard passes; RC8 does not waive that requirement. Only a qualified full-export sync uses a `Private-source:` trailer.

The published candidate's source archive and checksum come from its public commit. Local builds, release publication and deployment of the hosted website are separate checks.

`EXPORT_CANDIDATE_STATUS.json` identifies the public distribution, release and baseline for the container build. Its mechanical-completeness check confirms the build input contract; it is not approval of live deployment, invitations or a full private export. The release's separate [validation record](RC8_VALIDATION.md) states the checks and remaining limits.
