# Publishing the docs

The site has two parts, both published from the BOSS project's own server. The landing page at <https://boss.sean.network/> is static HTML, CSS, and JavaScript in `docs/landing/`. The manual at <https://boss.sean.network/docs/> is built with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/) from `docs/community/`. Pages outside the manual, such as the installer guide and examples, are linked on GitHub.

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

RC7 is an incremental release on the existing public RC6.1 history. Reviewed product changes and public guides are added as focused commits, preserving contributor ancestry. Reconstructed changes name their reviewed preparation commit with a `Prepared-from:` trailer; this is not a claim that the whole private source was exported.

A future full private-source export must first import all public contributor changes, retaining authors and `Imported-from:` provenance, and pass the export guard. That historical synchronization is still pending. A wholesale export that replaces the public tree remains refused until the guard passes; RC7 does not waive that requirement. Only a qualified full-export sync uses a `Private-source:` trailer.

The published candidate's source archive and checksum come from its public commit. Local builds, release publication and deployment of the hosted website are separate checks.
