# Community dependency and notice review — BOSS-300 / BOSS-293

**Status (2026-09-28): exact `0db60f6c` Community image inventory recorded;
PF-1 and PF-4 remain open.**
The source checkout and pristine x86 Linux images were inspected. The exact
source exports 1,880 files with zero mechanical blockers. These findings are
review inputs, not third-party notice clearance or a legal compatibility
decision.

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

## Frozen x86 image inventory (`1a8102a2`, review still open)

The clean VM rebuilt the default Community API, worker and web images from the
frozen export. Their local image IDs are API
`sha256:b39f7e594bcc59c0f636b28ed66abacf3234ee4e07efc929485493fa2f869c5f`,
worker `sha256:be6ff73f12695e2a6b38bf9e8d289a38f0bccc83d4b01608e401a26f6bb7ac06`,
and web `sha256:7fe7bbaf085468da6b96b94d2d18bc0f41387836df9b65e46dcacab6db7540c1`.
The default local PostgreSQL image ID is
`sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea`.
These are local image identities, not registry digests or immutable release
tags.

The [sanitized VM qualification report](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-ga-1a8102/qualification-report.md)
and [exact final-image inventory](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-ga-1a8102/qa-final-image/summary.json)
record the build and package observations. The API and worker each have 59 Python distributions; the web runtime has
103 unique pnpm packages. Globally installed npm inventories contain 207 API
and 204 web entries. Debian inventories contain 354 API and 88 web packages,
with zero unresolved `/usr/share/doc/*/copyright` paths in the reviewed
inventory. The API/worker and web images contain the root Apache `LICENSE`;
the API/worker contain the copied Node license. Linux sharp-libvips glibc and
musl 1.2.4 packages are present. The optional Redis and Infisical secrets
profile was not pulled or inventoried in this default-install run. Exact
inventory artifacts and terms review must accompany the VM qualification
report; these counts alone do not close the notice gate.

After safe uninstall/reinstall, rebuilt API, worker and web images had
different local IDs, but their package inventories were byte-identical to the
first-install inventories. The optional profile and actual CLI authentication
were not exercised. The next source candidate will need its own image inventory.

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

## Interim GA source export (`2744f7d0`; final image review pending)

The quarantined source candidate at `/tmp/boss-community-preflight-2744f7d0`
records source commit `2744f7d0bcd716a9b81b836310fad16cab8fea71` and
`mechanically_complete: true`, `blocker_count: 0`,
`ready_for_publication: false`. Its six license-named files are the root
`LICENSE`, `engine/LICENSE`, and one each in `packages/api-client`,
`packages/create-boss`, `packages/goldeneye`, and `packages/specpack`. The root
Apache text has SHA-256
`fe847534e6d3ab7e0155d31ae5604d30b4e57030bb7c2c1167356ddba54ed8bd`;
the Goldeneye kit's 875-byte `LICENSE` is a short Apache notice and full-text
link (SHA-256
`dab750002b8b619f8968f58a59b8ff99c1a212a5aea0d57adfa0553b994995ba`).
No `NOTICE` or `THIRD-PARTY-NOTICES` file was present in that source export.
This is a source-tree observation, not a finding that any particular dependency
requires a consolidated notice file or that the built images lack their own
license texts.

The candidate Dockerfile still assembles the API/worker runtime from Debian
`ffmpeg` and `libavfilter-extra`, `psycopg[binary]==3.2.9`, Playwright Chromium
matched to `playwright==1.60.0`, and globally installed Claude Code `2.1.197`
and Codex `0.158.0`. The web image installs the frozen pnpm lock, which includes
`caniuse-lite@1.0.30001793` and `@img/sharp-libvips` `1.2.4`. The source export
does not contain the downloaded wheels, npm packages, Debian files or browser
binaries. Therefore it cannot establish their exact final-image notice paths or
the redistribution disposition. The Goldeneye kit in the source export has
install scripts that reference third-party container images (for example
Traefik `v3.3`, Keycloak `26.0`, and MinIO `latest`); those image layers are not
bundled in this source candidate and need separate inventory if distributed as
part of a later kit artifact.

Version-matched installed package files provide narrower, authoritative text
evidence:

| Package | Observed bundled text | Limit of this evidence |
|---|---|---|
| `caniuse-lite@1.0.30001793` | The local pnpm package's `LICENSE` is the CC-BY-4.0 text, SHA-256 `fd3a263fe19ed8faa9068b43abaebafc02c77897b0c6fc09abc04bb592e5f16e`. Its `package.json` declares `CC-BY-4.0`, matching the frozen x86 web inventory. | This resolves the versioned license-text identity in the inspected checkout. Verify the file and any required attribution in the final web image and shipped notice set. |
| `psycopg-binary==3.2.9` | The local macOS wheel has `psycopg_binary-3.2.9.dist-info/licenses/LICENSE.txt`, SHA-256 `e3a994d82e644b03a792a930f574002658412f62407f5fee083f2555c5f23118`, containing LGPLv3 text. Its wheel `RECORD` lists 11 bundled `.dylibs` including `libpq`, OpenSSL and Kerberos-related libraries. The frozen x86 API/worker inventory confirms the same package version and LGPL metadata. | The macOS wheel is not the Linux wheel in the release image. Its single Psycopg license file does not establish the notices or source duties for every bundled Linux native library. |

The earlier frozen x86 inventory already resolves the blank
`qrcode-terminal` metadata field to an Apache-2.0 `LICENSE` file in the
inspected Node image. It does not clear the final rebuilt image or other npm
packages. No new conclusion about license compatibility follows from the
versioned text checks above.

The remaining counsel and release-image decisions are concrete: Claude Code
CLI redistribution terms; GPL-enabled Debian ffmpeg and related source duties;
LGPL and bundled-library materials for the Linux `psycopg-binary` wheel and
Linux sharp/libvips packages; `caniuse-lite` attribution; the full Playwright
Chromium notice set; and global Codex CLI native/dependency notices. The final
image inventory must also confirm Node and `qrcode-terminal` license paths and
separately cover the optional Redis/Infisical profile if that profile ships.

The later `4ab3e729` clean-VM global npm inventory also surfaced
`spdx-exceptions@2.5.0` under CC-BY-3.0 in API and web. Its versioned credit
and license text need the same final-image attribution disposition as
`caniuse-lite`; this was absent from the earlier review list. The
[supplemental notice review](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-license-notice-review-62aa369d.md)
records the observed package and the remaining exact-image checks.

The Community API and worker image now install the official
`@xai-official/grok@1.0.41` CLI and its matching Linux architecture package.
The npm registry metadata identifies both as Apache-2.0. The pinned
Linux-x64 and Linux-arm64 npm tarballs each advertise
`THIRD_PARTY_NOTICES.md`; verify the relevant notice is retained in each final
image and review its contents.
This repairs a source/runtime mismatch: the documentation required a Grok
login inside the API container even though the image previously installed
only Claude Code and Codex.

## Exact `0db60f6c` x86 runtime image inventory

The pristine Ubuntu 24.04 x86 build produced local image IDs API
`sha256:4e72c3ebca8efb6f84236f7855fae0f1c929097d1e67539e2048270a9be0d974`,
worker
`sha256:934751be75b304268de88877d56876e63c766648b19185551238334cf4cfa225`,
web
`sha256:6baef616c4c2ad883fabd083c37a83483cdfe110ac5b9f4eef27695571fc9c99`,
and PostgreSQL
`sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea`.
These are local image identities, not published image digests. The
[package summary](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-ga-0db60f6c/qa-inventory-summary.json)
records 59 Python distributions in each API/worker image, 103 web pnpm
packages, 210/210/204 global npm entries, and 354/354/88 Debian packages.
Every inventoried Debian package has an installed copyright notice.

Both API and worker images run Grok `1.0.41`, Claude Code `2.1.197`, and Codex
`0.158.0`. The installed Grok Linux x64 platform package retains
`THIRD_PARTY_NOTICES.md` (SHA-256
`e8785a6098a7ee780cd2db35745b8e53061cfb1b6da19147a308579466ea4e50`).
The Grok top-level package root has no separate license file. In both images,
`qrcode-terminal` retains its Apache license file (SHA-256
`b3c7a2fadb2515b8106eae58439a4b9c0581a4eaa88d6a265701f8d4dd7dadb8`),
and `setuptools==79.0.1` retains a license file (SHA-256
`86da0f01aeae46348a3c3d465195dc1ceccde79f79e87769a64b8da04b2a4741`)
despite incomplete license metadata. The
[exact-image notice paths and hashes](/Users/sean/.codex/visualizations/2026/09/28/01a0e916-4f7e-7c91-b3e6-a8bbeeeca9bb/community-ga-0db60f6c/notices/qa-notices.txt)
are retained outside the public export.

This inventory narrows file-presence questions. It does not settle Claude Code
or Grok redistribution terms, CC-BY credit, GPL/LGPL source and notice duties,
the Linux Psycopg binary, sharp/libvips, Chromium, or optional Redis/Infisical
profiles. A named reviewer must approve the exact image and notice set before
PF-1 and PF-4 close.

## Required disposition before PF-1/PF-4 can close

1. Repeat the package and notice inventory on the final x86 Linux web, API and
   worker images. Distinguish runtime packages from build-only tools and
   optional platforms.
2. Check each distributed package's license and included text. Preserve required
   copyright, license and attribution notices in the shipped artifact; review
   `psycopg[binary]`, sharp/libvips, `caniuse-lite`, `spdx-exceptions`, packaged coding CLIs,
   Debian ffmpeg, Node and Playwright browser binaries explicitly with counsel.
3. Review the Community `LICENSE`, package metadata, Goldeneye kit terms and
   any final `NOTICE` against the exact export. Record the reviewer, evidence
   hashes and disposition on Plane BOSS-300 and BOSS-293.

No conclusion about redistribution obligations is drawn from the preliminary
metadata alone.
