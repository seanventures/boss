"""BOSSAPP-808: an org connects SEVERAL GitHub accounts with tokens (a personal account and an
organisation). Each token keeps its own repositories; a build uses the token that covers its repo,
never "whichever was stored last". The single-token behaviour of Identity › GitHub is unchanged."""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.identity import github_setup

from .test_identity_github_setup import PAT, _clone, _injected, _run, env  # noqa: F401 — the fixture

SECOND = "github_pat_SECONDACCOUNT00000000000000000"


def _active(c) -> int:
    return int(c.execute(text("SELECT count(*) FROM identity_vault_items WHERE kind = 'legacy_pat' "
                              "AND owner_principal_id IS NULL AND status = 'active'")).scalar())


def test_a_second_account_is_kept_and_each_repo_uses_its_own_accounts_token(env):  # noqa: F811
    with env.db() as c:
        github_setup.store_org_token(token=PAT, repos=["acme/web"], org_id="org_boss", db=c, by=env.alice)
        github_setup.store_org_token(token=SECOND, repos=["globex/api"], org_id="org_boss", db=c,
                                     by=env.alice, check=False, keep_others=True, login="globex-bot")
        assert _active(c) == 2
        assert {t["login"] for t in github_setup.org_tokens(c, "org_boss")} == {"octo-admin", "globex-bot"}
        assert github_setup.control_plane_token(c, "acme", "web") == PAT
        assert github_setup.control_plane_token(c, "globex", "api") == SECOND
        assert github_setup.control_plane_token(c, "acme", "api") is None
        assert {t["login"] for t in github_setup.mode(c, "org_boss")["tokens"]} == {"globex-bot", "octo-admin"}
    shim, up, proxy = _run(env)
    assert _clone(proxy, shim).status == 200 and _injected(up) == PAT
    other, up2, proxy2 = _run(env, repos=("globex/api",), run="run_ct:gh2")
    assert _clone(proxy2, other, "globex/api").status == 200 and _injected(up2) == SECOND
    third, up3, proxy3 = _run(env, repos=("acme/secret",), run="run_ct:gh3")
    resp = _clone(proxy3, third, "acme/secret")
    assert resp.status == 403 and b"repo_outside_org_token" in resp.body and up3.calls == []


def test_the_same_login_again_replaces_only_its_own_token_and_keeps_its_repos(env):  # noqa: F811
    with env.db() as c:
        github_setup.store_org_token(token=PAT, repos=["acme/web"], org_id="org_boss", db=c, by=env.alice)
        github_setup.store_org_token(token=SECOND, repos=["globex/api"], org_id="org_boss", db=c,
                                     by=env.alice, check=False, keep_others=True, login="globex-bot")
        github_setup.store_org_token(token="github_pat_GLOBEXNEW0000000000000000000", repos=["globex/site"],
                                     org_id="org_boss", db=c, by=env.alice, check=False, keep_others=True,
                                     login="globex-bot")
        tokens = github_setup.org_tokens(c, "org_boss")
        assert len(tokens) == 2
        globex = next(t for t in tokens if t["login"] == "globex-bot")
        assert set(globex["repos"]) == {"globex/api", "globex/site"}
        assert github_setup.control_plane_token(c, "acme", "web") == PAT


def test_identity_githubs_paste_still_replaces_the_org_token(env):  # noqa: F811
    with env.db() as c:
        github_setup.store_org_token(token=SECOND, repos=["globex/api"], org_id="org_boss", db=c,
                                     by=env.alice, check=False, keep_others=True, login="globex-bot")
        github_setup.store_org_token(token=PAT, repos=["acme/web"], org_id="org_boss", db=c, by=env.alice)
        assert _active(c) == 1
        assert github_setup.org_token(c, "org_boss")["login"] == "octo-admin"


def test_removing_one_account_leaves_the_other(env):  # noqa: F811
    with env.db() as c:
        github_setup.store_org_token(token=PAT, repos=["acme/web"], org_id="org_boss", db=c, by=env.alice)
        second = github_setup.store_org_token(token=SECOND, repos=["globex/api"], org_id="org_boss", db=c,
                                              by=env.alice, check=False, keep_others=True, login="globex-bot")
        assert github_setup.remove_org_token(org_id="org_boss", db=c, by=env.alice, token_id=second["id"])
        assert [t["login"] for t in github_setup.org_tokens(c, "org_boss")] == ["octo-admin"]
        github_setup.add_repo_to_org_token(repo="acme/api", org_id="org_boss", db=c)
        assert set(github_setup.org_token(c, "org_boss")["repos"]) == {"acme/web", "acme/api"}
