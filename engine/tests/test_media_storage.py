"""Fetching media from whatever holds it.

The direct-link path is the one with no credential and no provider between BOSS and an
operator-supplied URL, so it carries the guards and it gets the tests.
"""
import os

import pytest

from boss_engine.services import media_storage as s
from boss_engine.services.media_providers import MediaProviderError


def test_a_link_is_placed_and_described_without_fetching_anything(tmp_path):
    """What a console field or a workflow's first step needs: is this usable, and by what?"""
    google = s.describe("https://drive.google.com/file/d/1AbC_dEfGhIjKlMnOpQ/view")
    assert google["provider"] == "google"
    assert google["needs_connection"] is True
    assert google["fetchable_today"] is True

    direct = s.describe("https://example.com/clip.mp4")
    assert direct["provider"] == "http"
    assert direct["needs_connection"] is False


def test_an_unbuilt_provider_says_what_to_do_instead_of_not_implemented():
    """A refusal that names the workaround is the difference between a blocked operator and one
    who copies the file to Drive and carries on."""
    onedrive = s.describe("https://1drv.ms/v/s!AbCdEf")
    assert onedrive["provider"] == "microsoft"
    assert onedrive["fetchable_today"] is False
    assert "Google Drive" in onedrive["note"] and "Graph" in onedrive["note"]

    bucket = s.describe("s3://my-videos/raw/clip.mp4")
    assert bucket["fetchable_today"] is False
    assert "pre-signed" in bucket["note"]


def test_the_legacy_google_connection_name_is_still_resolved():
    """Every Google connection that exists today is keyed `youtube_default`.

    `channel_oauth` created them that way, when the only reason to connect Google was to publish
    to YouTube. Looking only for `google_default` would leave a working connection sitting in the
    table unlooked-for, in every install that already has one — which is exactly what the
    settings.github_token change did to preview clones.
    """
    assert s.default_connection_keys("google") == ("google_default", "youtube_default")
    # Providers added later have no legacy name to carry.
    assert s.default_connection_keys("microsoft") == ("microsoft_default",)
    assert s.default_connection_keys("s3") == ("s3_default",)


def test_writing_to_a_read_only_provider_is_refused_before_anything_is_uploaded(tmp_path):
    """A direct HTTPS link has nothing to write to and no account to write as."""
    target = tmp_path / "out.mp4"
    target.write_bytes(b"x")
    with pytest.raises(s.MediaStorageError, match="read-only"):
        s.store(None, None, project_id="p", local_path=str(target), name="out.mp4",
                provider_key="http")


def test_a_fetch_dials_the_address_it_checked_and_not_the_next_lookup(tmp_path, monkeypatch):
    """DNS rebinding is a second lookup.

    The check resolves example.com to a public address. The lookup after that — the one the
    HTTP client would do itself — answers with the metadata address. The connection has to use
    the first answer. Dialing the second is the fetch the check exists to stop.
    """
    import socket

    from boss_engine.services.media_providers import ResolvedLink

    real_getaddrinfo = socket.getaddrinfo
    calls = {"n": 0}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "example.com":
            calls["n"] += 1
            ip = "8.8.8.8" if calls["n"] == 1 else "169.254.169.254"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    seen: dict[str, tuple] = {}

    def fake_connect(address, *args, **kwargs):
        seen["address"] = address
        raise OSError("stopped before a socket was opened")

    monkeypatch.setattr(socket, "create_connection", fake_connect)
    link = ResolvedLink("http", file_ref="https://example.com/clip.mp4",
                        original_url="https://example.com/clip.mp4")
    with pytest.raises(s.MediaStorageError):
        s._fetch_direct(link, dest_dir=str(tmp_path))
    assert seen["address"][0] == "8.8.8.8"
    assert seen["address"][0] != "169.254.169.254"
    assert not list(tmp_path.iterdir())


def test_a_direct_fetch_refuses_shared_address_space(tmp_path):
    """100.64.0.0/10 is not marked private, and it is where metadata and tailnets live."""
    from boss_engine.services.media_providers import ResolvedLink

    link = ResolvedLink(
        "http", file_ref="http://100.100.100.200/latest/meta-data/",
        original_url="http://100.100.100.200/latest/meta-data/")
    with pytest.raises(MediaProviderError, match="BOSS's own network"):
        s._fetch_direct(link, dest_dir=str(tmp_path))
    assert not list(tmp_path.iterdir())


def test_a_direct_fetch_refuses_an_address_inside_our_own_network(tmp_path):
    """Checked again at fetch time, not only when the link was placed.

    Between placing and fetching, a name can resolve to something else — which is a DNS-rebinding
    fetch of the very metadata endpoint the first check refused.
    """
    from boss_engine.services.media_providers import ResolvedLink

    link = ResolvedLink("http", file_ref="http://169.254.169.254/latest/meta-data/",
                        original_url="http://169.254.169.254/latest/meta-data/")
    with pytest.raises(MediaProviderError, match="BOSS's own network"):
        s._fetch_direct(link, dest_dir=str(tmp_path))
    assert not list(tmp_path.iterdir())


def test_a_redirect_is_refused_rather_than_followed(tmp_path, monkeypatch):
    """A public URL that 302s to 169.254.169.254 is the address check wearing a hat."""
    import httpx

    def handler(request):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    _patch_client(monkeypatch, handler)
    from boss_engine.services.media_providers import ResolvedLink

    link = ResolvedLink("http", file_ref="https://example.com/clip.mp4",
                        original_url="https://example.com/clip.mp4")
    with pytest.raises(s.MediaStorageError, match="redirect"):
        s._fetch_direct(link, dest_dir=str(tmp_path))
    assert not list(tmp_path.iterdir())


def test_an_oversized_file_is_refused_on_its_claim_and_again_while_streaming(
        tmp_path, monkeypatch):
    """content-length is a claim, and a chunked response makes none at all."""
    import httpx

    from boss_engine.services.media_providers import ResolvedLink

    link = ResolvedLink("http", file_ref="https://example.com/clip.mp4",
                        original_url="https://example.com/clip.mp4")

    # Claimed, and refused before a byte is written.
    _patch_client(monkeypatch, lambda r: httpx.Response(
        200, headers={"content-length": str(20 * 1024**3)}, content=b"x"))
    with pytest.raises(s.MediaStorageError, match="over BOSS's"):
        s._fetch_direct(link, dest_dir=str(tmp_path))

    # Unclaimed, and refused part-way through. A STREAMING body, because httpx derives
    # content-length from a bytes body — which is exactly the case a chunked response does not
    # give us, and the reason the streaming check has to exist at all. The limit is lowered for
    # the test rather than streaming 8 GB to prove it.
    monkeypatch.setattr(s, "MAX_DIRECT_BYTES", 1024)
    _patch_client(monkeypatch, lambda r: httpx.Response(
        200, content=iter([b"y" * 2000, b"y" * 2000, b"y" * 2000])))
    with pytest.raises(s.MediaStorageError, match="part-way through"):
        s._fetch_direct(link, dest_dir=str(tmp_path))
    # Nothing half-written survives: a truncated video probes and plays for a second, which is
    # worse than no video because it looks like a source somebody could edit.
    assert not list(tmp_path.iterdir())


def test_an_empty_response_is_not_a_download(tmp_path, monkeypatch):
    import httpx

    from boss_engine.services.media_providers import ResolvedLink

    _patch_client(monkeypatch, lambda r: httpx.Response(200, content=b""))
    link = ResolvedLink("http", file_ref="https://example.com/clip.mp4",
                        original_url="https://example.com/clip.mp4")
    with pytest.raises(s.MediaStorageError, match="empty"):
        s._fetch_direct(link, dest_dir=str(tmp_path))
    assert not list(tmp_path.iterdir())


def test_a_direct_fetch_that_works_lands_a_file_with_a_safe_name(tmp_path, monkeypatch):
    """The filename comes out of an operator-supplied URL, so it may not choose where it lands."""
    import httpx

    from boss_engine.services.media_providers import ResolvedLink

    _patch_client(monkeypatch, lambda r: httpx.Response(200, content=b"video-bytes" * 100))
    link = ResolvedLink("http", file_ref="https://example.com/a/../../etc/passwd",
                        original_url="https://example.com/a/../../etc/passwd")
    result = s._fetch_direct(link, dest_dir=str(tmp_path))
    assert result["bytes"] == len(b"video-bytes" * 100)
    # Landed inside dest_dir under a flattened name, not at /etc/passwd.
    assert os.path.dirname(os.path.realpath(result["path"])) == os.path.realpath(str(tmp_path))
    assert "/" not in result["name"]


#: The GENUINE client, captured at import. Resolving it inside _patch_client would pick up a
#: factory installed by an earlier call in the same test, and the nested factory then overwrites
#: the new handler's transport with the old one — so the second patch in a test silently keeps
#: replaying the first response.
_REAL_HTTPX_CLIENT = None


def _patch_client(monkeypatch, handler):
    """Point httpx at a transport instead of the network, keeping streaming behaviour real."""
    global _REAL_HTTPX_CLIENT
    import httpx

    if _REAL_HTTPX_CLIENT is None:
        _REAL_HTTPX_CLIENT = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_HTTPX_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
