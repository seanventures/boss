# Community dependency and notice review — BOSS-300 / BOSS-293

**Status (2026-09-28): preliminary inventory; PF-1 and PF-4 remain open.**
This records packages observed in the integration checkout. It is not an audit
of the final Linux images, a third-party notice, or a legal compatibility
decision. Re-run against the exact export and built images before publication.

## Evidence collected

- `pnpm licenses list --recursive --json` on the macOS integration checkout
  reported 98 installed packages: MIT 79, Apache-2.0 7, ISC 7, and one each of
  0BSD, BSD-2-Clause, BSD-3-Clause, CC-BY-4.0 and LGPL-3.0-or-later.
  The LGPL package was the macOS optional
  `@img/sharp-libvips-darwin-arm64@1.2.4`; the exact Linux image may include
  a different platform package. The build uses `sharp@0.34.5`.
- Local installed metadata for the 19 direct API/worker requirements identifies
  `psycopg==3.2.9` as LGPLv3 and `reportlab==4.5.1` as a BSD-style license with
  its own terms. The requirements also install `psycopg[binary]`, so review the
  binary distribution and its bundled libraries, not only the pure Python
  package. This count excludes transitive packages and base image contents.
- `caniuse-lite@1.0.30001793` is present under CC-BY-4.0. Its upstream
  [README](https://github.com/browserslist/caniuse-lite/blob/main/README.md)
  gives attribution guidance. The
  [sharp-libvips notices](https://github.com/lovell/sharp-libvips/blob/main/THIRD-PARTY-NOTICES.md)
  list bundled libraries with their separate terms. The
  [Psycopg installation guide](https://www.psycopg.org/psycopg3/docs/basic/install.html)
  describes the self-contained binary package. These upstream references are
  review inputs; the precise distributed versions and files still need
  confirmation from the final image.

## Required disposition before PF-1/PF-4 can close

1. Inventory the exact x86 Linux web, API and worker images, including Node,
   Python, operating-system packages and bundled binary libraries. Distinguish
   runtime packages from build-only tools and optional platforms.
2. Check each distributed package's license and included text. Preserve required
   copyright, license and attribution notices in the shipped artifact; review
   `psycopg[binary]`, sharp/libvips and `caniuse-lite` explicitly with counsel.
3. Review the Community `LICENSE`, package metadata, Goldeneye kit terms and
   any final `NOTICE` against the exact export. Record the reviewer, evidence
   hashes and disposition on Plane BOSS-300 and BOSS-293.

No conclusion about redistribution obligations is drawn from the preliminary
metadata alone.
