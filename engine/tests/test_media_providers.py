"""Which provider a pasted link belongs to, and what BOSS refuses to follow.

The link is an UNTRUSTED INPUT: a workflow input somebody pasted, or a URL a model put in an
edit plan. So the interesting tests here are the refusals, and the most important one is not
about media at all — it is about a link that points back inside the network BOSS runs in.
"""
import pytest

from boss_engine.services import media_providers as m


# --- placing a link ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected_id", [
    ("https://drive.google.com/file/d/1AbC_dEfGhIjKlMnOpQ/view?usp=sharing",
     "1AbC_dEfGhIjKlMnOpQ"),
    ("https://drive.google.com/file/d/1AbC_dEfGhIjKlMnOpQ/view", "1AbC_dEfGhIjKlMnOpQ"),
    ("https://drive.google.com/open?id=1AbC_dEfGhIjKlMnOpQ", "1AbC_dEfGhIjKlMnOpQ"),
    ("https://docs.google.com/presentation/d/1AbC_dEfGhIjKlMnOpQ/edit", "1AbC_dEfGhIjKlMnOpQ"),
])
def test_a_google_share_link_yields_its_file_id(url, expected_id):
    """All four shapes are links Drive's own UI produces, so all four must work."""
    link = m.resolve_link(url)
    assert link.provider_key == "google"
    assert link.file_ref == expected_id


@pytest.mark.parametrize("url", [
    "https://1drv.ms/v/s!AbCdEfGhIjKlMnO",
    "https://onedrive.live.com/?cid=ABC123&id=ABC123%21456",
    "https://contoso-my.sharepoint.com/:v:/g/personal/someone_contoso_com/AbCdEf",
    "https://contoso.sharepoint.com/sites/marketing/Shared%20Documents/clip.mp4",
])
def test_a_microsoft_link_is_placed_as_microsoft_not_google(url):
    """OneDrive is Microsoft Graph, not a Google account, and the two get conflated constantly.

    Placing one of these as `google` would send it to a connection that cannot read it however
    many Drive scopes that connection holds — and the failure would arrive as an opaque 404 from
    Drive rather than as "that is a different provider".
    """
    link = m.resolve_link(url)
    assert link.provider_key == "microsoft"
    # A share URL is an opaque token that Graph takes whole; there is no id to parse out, and no
    # filename either, so the step must expect to learn the name on fetch.
    assert link.file_ref == url
    assert link.name_unknown is True


@pytest.mark.parametrize("url,bucket,key", [
    ("s3://my-videos/raw/interview.mp4", "my-videos", "raw/interview.mp4"),
    ("https://my-videos.s3.eu-west-1.amazonaws.com/raw/interview.mp4",
     "my-videos", "raw/interview.mp4"),
    ("https://s3.eu-west-1.amazonaws.com/my-videos/raw/interview.mp4",
     "my-videos", "raw/interview.mp4"),
    ("https://abc123.r2.cloudflarestorage.com/my-videos/raw/interview.mp4",
     "my-videos", "raw/interview.mp4"),
    ("https://s3.us-west-002.backblazeb2.com/my-videos/raw/interview.mp4",
     "my-videos", "raw/interview.mp4"),
])
def test_the_s3_family_gives_up_a_bucket_and_a_key(url, bucket, key):
    """Virtual-hosted and path style, across four vendors that all speak the same API.

    The bucket is not optional the way it is for Drive: a key without it names nothing.
    """
    link = m.resolve_link(url)
    assert (link.provider_key, link.container, link.file_ref) == ("s3", bucket, key)


def test_an_ordinary_https_link_is_a_direct_file():
    link = m.resolve_link("https://example.com/videos/clip.mp4")
    assert link.provider_key == "http"
    assert link.file_ref == "https://example.com/videos/clip.mp4"


# --- refusing a link -------------------------------------------------------------------------

@pytest.mark.parametrize("host", [
    "localhost",                 # the console and the API itself
    "127.0.0.1",
    "169.254.169.254",           # cloud instance metadata
    "10.0.0.5",
    "192.168.1.10",
    "172.16.4.4",
    "0.0.0.0",
    "[::1]",
])
def test_a_link_pointing_back_inside_the_network_is_refused(host):
    """THE ONE THAT MATTERS.

    "Fetch this video" must not fetch http://169.254.169.254/latest/meta-data/iam/ and hand the
    control plane's own cloud role to whoever pasted the link, and it must not reach the BOSS API
    on localhost as a caller with no authentication to answer for. The link comes from a workflow
    input or a model's plan, so neither is a hypothetical.
    """
    with pytest.raises(m.MediaProviderError, match="BOSS's own network"):
        m.resolve_link(f"http://{host}/latest/meta-data/iam/security-credentials/")


def test_a_scheme_that_is_not_a_web_fetch_is_refused():
    """`file://` would turn a pasted link into "read any path on the control plane"."""
    for url in ("file:///etc/passwd", "gopher://example.com/1",
                "data:video/mp4;base64,AAAA", "ftp://example.com/clip.mp4"):
        with pytest.raises(m.MediaProviderError, match="http and https"):
            m.resolve_link(url)


def test_a_link_that_names_no_file_is_refused_rather_than_guessed_at():
    for url in ("https://drive.google.com/drive/my-drive",     # a folder view, no id
                "https://example.com",                        # a host and nothing else
                "s3://just-a-bucket",
                ""):
        with pytest.raises(m.MediaProviderError):
            m.resolve_link(url)


def test_an_implausibly_long_link_is_refused_before_anything_parses_it():
    with pytest.raises(m.MediaProviderError, match="implausibly long"):
        m.resolve_link("https://example.com/" + "a" * 3000)


# --- capabilities, not provider names ---------------------------------------------------------

def test_a_capability_maps_to_whatever_the_provider_calls_it():
    """A skill asks for storage.read. What that means in scopes is the provider's business.

    This is the split that lets one skill spec serve four providers, and it replaces
    `requires_connection: google` — a provider name in a policy field, enforced nowhere.
    """
    google = m.provider("google")
    assert "https://www.googleapis.com/auth/drive" in google.scopes_for((m.CAP_READ,))
    microsoft = m.provider("microsoft")
    assert microsoft.scopes_for((m.CAP_WRITE,)) == ("offline_access", "Files.ReadWrite.All")
    # Deduplicated across capabilities: Drive's one scope covers read and write, and asking for
    # both must not request it twice.
    assert google.scopes_for((m.CAP_READ, m.CAP_WRITE)) == (
        "https://www.googleapis.com/auth/drive",)


def test_asking_a_provider_for_something_it_cannot_do_fails_at_connect_time():
    """Not later, at the call, with a provider error nobody can trace back to a consent screen.

    S3 cannot publish a video and a direct link cannot be written to. Both are true of the
    provider, so both are knowable before a connection is ever minted.
    """
    with pytest.raises(m.MediaProviderError, match="cannot do"):
        m.provider("s3").scopes_for((m.CAP_PUBLISH,))
    with pytest.raises(m.MediaProviderError, match="cannot do"):
        m.provider("http").scopes_for((m.CAP_WRITE,))


def test_a_direct_link_is_read_only_and_needs_no_connection():
    """There is nothing to write to and no account to write as."""
    http = m.provider("http")
    assert http.capabilities == (m.CAP_READ,)
    assert http.auth_style == m.AUTH_NONE
    assert http.credential_fields == ()


def test_every_provider_declares_how_to_connect_it():
    """A connect screen that offers one box labelled "credential" for four providers is a
    connect screen that gets filled in wrong. Each provider names its own fields."""
    for entry in m.PROVIDERS.values():
        assert entry.capabilities, entry.key
        assert entry.setup_hint, entry.key
        if entry.auth_style == m.AUTH_OAUTH2_REFRESH:
            assert entry.auth_endpoint and entry.token_endpoint, entry.key
            assert entry.credential_fields, entry.key
        if entry.auth_style != m.AUTH_NONE:
            assert entry.credential_fields, entry.key


def test_no_provider_entry_carries_a_credential():
    """A provider describes how to ASK for one. Holding it here would put a secret in source.

    This is the rule that makes the registry safe to extend: there is no field to put a token in,
    so a new provider cannot repeat what settings.github_token did to preview clones.
    """
    import dataclasses

    for entry in m.PROVIDERS.values():
        for f in dataclasses.fields(entry):
            name = f.name.lower()
            assert not any(word in name for word in ("secret_value", "token_value", "password")), \
                f"{entry.key}.{f.name} looks like somewhere a credential would go"
        # credential_fields NAMES what to ask for; it must never hold the answers.
        for want in entry.credential_fields:
            assert isinstance(want, str) and len(want) < 40
