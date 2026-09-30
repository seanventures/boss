# Publishing the docs

This site is built with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/) from `docs/community/` and published to GitHub Pages at <https://seanventures.github.io/boss/>. Pages outside the manual, such as the installer guide and examples, are linked on GitHub.

## Build locally

From the repository root, install `mkdocs` and `mkdocs-material`, then run:

```bash
mkdocs build -f mkdocs.community.yml --strict
```

Use `mkdocs serve -f mkdocs.community.yml` for a live preview while you edit.

## Deploy

The `Docs site` workflow in `.github/workflows/pages.yml` builds the site in strict mode and deploys it to GitHub Pages on every push to `main` that touches the docs. Pull requests build the site without deploying it. Pages must be set to deploy from GitHub Actions in the repository settings.

## Installer pin

The one-line installer is pinned to `v1.0-rc1`, commit `7b88a18a2b4cd0b14b09a47fc415f7ffd4580066`. Update the pin in the README, the quickstart, and the installer guide together when a new release is cut.
