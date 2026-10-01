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

The commit-pinned GitHub command in the installer guide names one release commit. Update that pin when a new release is cut.
