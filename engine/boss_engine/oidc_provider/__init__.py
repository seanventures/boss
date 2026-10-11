"""BOSS as an OpenID Connect provider for the apps it builds (F6). Stream S6.

Spec: docs/product/APP_TEMPLATES_AND_SECURITY_LOOP.md §6; the identity properties it reuses are
docs/product/IDENTITY.md's (D35 for the signing algorithm).

  util       ids, hashing, lifetimes, the protocol error and the audit helper
  keys       the `oidc` signing-key family (RS256 default, EdDSA per client), per org, in its own
             table: no BOSS-internal verifier can find these keys, and no BOSS key signs here
  clients    the client registry, redirect-URI rules and client authentication
  pools      user pools, subjects (pairwise by default), claims and app roles
  consent    per (subject, client, scopes) consent and the consent form's CSRF token
  tokens     grants, codes, refresh families, ID and access tokens, and their verification
  protocol   discovery, JWKS, authorize, consent, token, userinfo, revocation, end_session
  pages      the hosted pages (error, consent, signed out): strict CSP, no framing

Framework-free: apps/api/app/oidc_routes.py turns requests into calls here and Replies into
responses.
"""
