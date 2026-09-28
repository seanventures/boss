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
  field. The inspected Node 20 base image does include that package's
  Apache-2.0 `LICENSE` at
  `/usr/local/lib/node_modules/npm/node_modules/qrcode-terminal/LICENSE`
  (SHA-256 `b3c7a2fadb2515b8106eae58439a4b9c0581a4eaa88d6a265701f8d4dd7dadb8`).
  A blank metadata field is therefore not proof that its license text is
  absent. Confirm the path in final images and review CLI redistribution with
  counsel.
- The API image has 354 Debian packages and the web image 88. Installed
  `/usr/share/doc/*/copyright` files resolve for all inventoried packages.
  API Debian `ffmpeg` 5.1.9 reports GPL version 2 or later and an
  `--enable-gpl` build with libx264 and libx265. The Playwright browser tree
  has a headless-shell notice and ffmpeg LGPL notice; the full Chromium tree
  lacked a general LICENSE/NOTICE in the inspected paths. API copies a Node
  binary but did not copy the Node license file present in the web image.

## Current source follow-up

The integration source now copies the Node distribution license alongside its
binary. The Community Dockerfile also copies the reviewed root Apache-2.0
`LICENSE` into the API/worker and web runtime images. Verify those paths in the
final built images; source-level checks do not prove an image was rebuilt.
The inspected local `caniuse-lite` package root includes its CC-BY-4.0
`LICENSE`; verify that package's attribution and exact final-image path.
These additions do not resolve the separate third-party terms and notice
review below.

The Community Dockerfile pins the reviewed multi-architecture Docker Hub base
indexes for `python:3.11-slim-bookworm` (`sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b`)
and `node:20-bookworm-slim` (`sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0`).
These pins identify the base images; apt and package-registry downloads still
require the final image digests and inventory for release review.

The interim Debian inventory found a copyright text for every installed
package (354 API, 88 web). The `qrcode-terminal` metadata omission is resolved
to a present Apache-2.0 text in the inspected Node base image. Neither result
substitutes for checking the final rebuilt images.

The remaining file-level gaps are more specific: the inspected prebuilt
`sharp-libvips` package roots have a README listing bundled libraries but no
separate `LICENSE`, `COPYING`, or `THIRD-PARTY-NOTICES` file; the inspected
Playwright full Chromium tree has only a Widevine license in its browser
directory, while the headless-shell and ffmpeg trees have their own notices.
Identify the correct versioned notice texts and their distribution path before
declaring the image notice set complete.

Counsel still needs to dispose of Claude Code CLI redistribution terms, the
Debian GPL-enabled ffmpeg build and any related source duties, the bundled
native libraries in `psycopg-binary` and `sharp-libvips`, CC-BY attribution for
`caniuse-lite`, and the Playwright browser notice set. Codex CLI Apache
metadata alone does not settle its bundled native dependencies.

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
