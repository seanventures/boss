# Publishing the docs

The repository provides a static landing page in `docs/landing/` and a manual built with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/) from `docs/community/`. The project's hosted website is <https://boss.sean.network/>. Its deployment is separate from a source release; a local build does not verify the content currently served there. Pages outside the manual, such as the installer guide and examples, are linked on GitHub.

## Build locally

From the repository root, install `mkdocs` and `mkdocs-material`, then run:

```bash
mkdocs build -f mkdocs.community.yml --strict
```

Use `mkdocs serve -f mkdocs.community.yml` for a live preview while you edit. The landing page is plain HTML; open `docs/landing/index.html` in a browser, or serve the combined build with `python3 -m http.server`.

## Build check

The `Docs site` workflow in `.github/workflows/pages.yml` only verifies the build. On pull requests and pushes to `main` that touch the docs, it builds the manual in strict mode under `_site/docs/` and copies the landing page to `_site/`, the same layout served at <https://boss.sean.network/>. It does not deploy anything; GitHub Pages is not the host.

## Installer pin

The release-pinned GitHub command in the installer guide selects one release tag. The installer resolves and records its commit. Update the tag when a new release is cut; use the recorded full SHA when comparing an installed checkout with validation evidence.

## Changes pushed to `main`

RC8 is an incremental release retaining the complete published RC6.2 history. Reviewed product changes and public guides are added as focused commits, preserving contributor ancestry. Reconstructed changes name their reviewed preparation commit with a `Prepared-from:` trailer; this is not a claim that the whole private source was exported.

A future full private-source export must first import all public contributor changes, retaining authors and `Imported-from:` provenance, and pass the export guard. That historical synchronization is still pending. A wholesale export that replaces the public tree remains refused until the guard passes; RC8 does not waive that requirement. Only a qualified full-export sync uses a `Private-source:` trailer.

The published candidate's source archive and checksum come from its public commit. Local builds, release publication and deployment of the hosted website are separate checks.

`EXPORT_CANDIDATE_STATUS.json` identifies the public distribution, release and baseline for the container build. Its mechanical-completeness check confirms the build input contract; it is not approval of live deployment, invitations or a full private export. The release's separate [validation record](RC8_VALIDATION.md) states the checks and remaining limits.
