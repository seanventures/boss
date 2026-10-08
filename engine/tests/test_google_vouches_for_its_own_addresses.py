"""Google may claim an invitation for addresses Google owns, with no domain listed by hand.

boss-1, 2026-10-08: an Admin added Google, invited a Gmail address and was still refused, because
an invitation is claimed only for an email in a domain listed on the provider. Google owns gmail.com
and googlemail.com, and its signed `hd` claim names the Workspace domain that owns an account.
Nothing here creates anyone: auto-join stays on the listed domains only.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from boss_engine.identity.social import vouches_for
from boss_engine.identity.sso_oidc import FederatedIdentity


class P:
    def __init__(self, preset, domains=()):
        self.preset, self.domains = preset, list(domains)


def ident(email, verified=True, hd=None):
    return FederatedIdentity(issuer="https://accounts.google.com", subject="1", email=email,
                             email_verified=verified, hosted_domain=hd)


@pytest.mark.parametrize("email", ["me@gmail.com", "Me@GoogleMail.com"])
def test_google_vouches_for_its_consumer_addresses_without_a_listed_domain(email):
    assert vouches_for(P("google"), ident(email))


def test_google_vouches_for_a_workspace_domain_only_when_its_signed_hd_names_it():
    assert vouches_for(P("google"), ident("ana@acme.dev", hd="acme.dev"))
    assert not vouches_for(P("google"), ident("ana@acme.dev"))              # no hd: a consumer account
    assert not vouches_for(P("google"), ident("ana@acme.dev", hd="evil.dev"))


def test_an_unverified_email_is_never_vouched_for():
    assert not vouches_for(P("google"), ident("me@gmail.com", verified=False))


@pytest.mark.parametrize("preset", ["github", "microsoft", "okta", "oidc", "saml", None])
def test_no_other_provider_vouches_for_gmail(preset):
    assert not vouches_for(P(preset), ident("me@gmail.com", hd="gmail.com"))


def test_a_listed_domain_still_works_for_every_provider():
    assert vouches_for(P("okta", ["acme.dev"]), replace(ident("ana@acme.dev"), issuer="https://acme.okta.com"))
