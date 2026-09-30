# Publishing the community docs

The docs have not been published yet. Pages in this manual link to files in seanventures/boss when those files live outside the manual.

This site config is ready to become GitHub Pages after the repository is public. It does not publish anything now.

## Build locally

From the repository root, install `mkdocs` and `mkdocs-material`, then run:

```bash
mkdocs build -f mkdocs.community.yml --strict
```

## GitHub Pages

GitHub Pages is a later step on seanventures/boss. Do not add a workflow that deploys the site now. Leave `site_url` unset until GitHub Pages is enabled, and fill it in only then.

## Pages in the nav

These pages are in `mkdocs.community.yml`:

- `use-cases.md`
- `walkthroughs.md`
- `interfaces.md`
- `terminal.md`
- `cli.md`
- `hostname.md`
- `remote-api.md`

## Installer pin

The one-line installer still uses `REPLACE_WITH_REVIEWED_40_CHARACTER_SHA`. Do not replace that placeholder as part of publishing these docs.
