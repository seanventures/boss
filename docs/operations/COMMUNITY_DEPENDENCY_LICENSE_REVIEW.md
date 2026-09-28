# Community dependency and notice review — BOSS-300 / BOSS-293

**Status (2026-09-28): interim image inventory; PF-1 and PF-4 remain open.**
The source checkout and the first x86 Linux export candidate were inspected.
That candidate failed first-install completion and has since changed. These
findings are review inputs, not third-party notice clearance or a legal
compatibility decision. Re-run against the final export and built images.

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

## First x86 image candidate (`327b58f2`, superseded)

The [sanitized VM report](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-vm-qualification-327b58f2.md)
records image identities and inspection limits. Its [Python and web package
inventory](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-runtime-licenses-327b58f2.json),
[global npm inventory](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-global-npm-licenses-327b58f2.json),
and [Debian notice inventory](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-os-notices-327b58f2.json)
are retained outside the proposed public tree.

- API and worker each have 59 Python distributions. `psycopg` and
  `psycopg-binary` report LGPLv3, `certifi` MPL-2.0, and `tqdm` MPL-2.0 AND
  MIT. `setuptools` lacks a license metadata field. The web runtime has 103
  unique pnpm packages, including both x64 glibc and musl
  `@img/sharp-libvips` 1.2.4 packages under LGPL-3.0-or-later. Their package
  roots have no separate LICENSE/NOTICE/COPYING file in the inspected depth;
  their READMEs list native library licenses.
- The API image also has 207 globally installed npm entries, including
  Claude Code 2.1.197 (`SEE LICENSE IN README.md`) and Codex 0.158.0
  (Apache-2.0). The Claude README points to commercial terms. Both API and
  web global npm trees contain `qrcode-terminal` without a license metadata
  field. Review CLI redistribution and missing metadata with counsel.
- The API image has 354 Debian packages and the web image 88. Installed
  `/usr/share/doc/*/copyright` files resolve for all inventoried packages.
  API Debian `ffmpeg` 5.1.9 reports GPL version 2 or later and an
  `--enable-gpl` build with libx264 and libx265. The Playwright browser tree
  has a headless-shell notice and ffmpeg LGPL notice; the full Chromium tree
  lacked a general LICENSE/NOTICE in the inspected paths. API copies a Node
  binary but does not copy the Node license file present in the web image.

Package metadata and notice-path searches do not establish redistribution
rights, source-offer compliance, or completeness for bundled native binaries.

## Required disposition before PF-1/PF-4 can close

1. Repeat the package and notice inventory on the final x86 Linux web, API and
   worker images. Distinguish runtime packages from build-only tools and
   optional platforms.
2. Check each distributed package's license and included text. Preserve required
   copyright, license and attribution notices in the shipped artifact; review
   `psycopg[binary]`, sharp/libvips, `caniuse-lite`, packaged coding CLIs,
   Debian ffmpeg, Node and Playwright browser binaries explicitly with counsel.
3. Review the Community `LICENSE`, package metadata, Goldeneye kit terms and
   any final `NOTICE` against the exact export. Record the reviewer, evidence
   hashes and disposition on Plane BOSS-300 and BOSS-293.

No conclusion about redistribution obligations is drawn from the preliminary
metadata alone.
