# BOSS Engine Public-Release Readiness

**Plane:** BOSS-293
**Current state:** private preparation only — this document is not approval to
make a repository public, publish a package, or advertise general availability.

The future public repository should carry the files in this directory at its
root after the history-preserving engine extraction. This kit makes the
remaining decisions and evidence explicit rather than treating a collection of
documents as a release.

## Hard gates

All of these must be true before a public flip:

- [ ] Product and legal have recorded the BOSS-300 licensing and OSS-boundary
      decision. The selected license, NOTICE, package metadata, website, and
      marketing language agree.
- [ ] The engine has been extracted into the approved **private** repository
      with preserved history, and the private-repository validation matrix is
      green.
- [ ] A second full-history secret scan of the extracted repository has
      completed with reviewed results. No secret, customer data, private
      hostname, or unapproved internal endpoint remains.
- [ ] Dependency and license audit evidence is attached to the release record;
      incompatible dependencies have been removed or received approved legal
      treatment.
- [ ] A security contact and disclosure route are configured in
      [SECURITY.md](./SECURITY.md). The current placeholder is intentionally a
      public-release blocker.
- [ ] A versioning/release owner and signing or provenance policy are approved.
- [ ] Two external design partners have self-served the canonical sample task
      and their friction items have been triaged (BOSS-292).
- [ ] The community scope, local-model, durable-workspace, role-pack, and
      skill-SDK claims are verified or removed from launch material.
- [ ] The release owner has approved public visibility and rollback/incident
      ownership.

## Repository hygiene carried by this kit

- [CONTRIBUTING.md](./CONTRIBUTING.md) defines contribution expectations
  without inventing a contributor-license agreement.
- [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md) defines a baseline community
  standard and escalation expectation.
- [SECURITY.md](./SECURITY.md) blocks publication until an actual reporting
  destination is provided.
- [RELEASING.md](./RELEASING.md) defines the release evidence and versioning
  contract.
- The .github directory contains bug, feature, and pull-request templates for
  the extracted repository.

## Evidence record

For each gate, record the date, owner, immutable artifact/commit/run URL, and
result in BOSS-293. A green local build is evidence for only its corresponding
technical check; it is not evidence of a legal decision, a secret-history
review, design-partner outcome, or authorization to change repository
visibility.

## Explicit non-goals

This kit does not:

- choose a license or change the current license;
- create a public GitHub repository, publication account, support inbox, or
  package-registry release;
- run a historical scan without the extracted repository and approved scanner
  configuration;
- represent unmerged code or unconfigured hosted services as shipped.
