from pathlib import Path

from boss_engine.services.official_canary import (
    hookwatch_is_stubbed,
    maybe_restore_for_command,
    restore_official_canary,
    restore_official_hookwatch,
)
from boss_engine.services.verification_target import prepare_web_verify_env


def _stub_tree(tmp_path: Path) -> Path:
    app = tmp_path / "hookwatch"
    (app / "src").mkdir(parents=True)
    (app / "e2e").mkdir()
    (app / "src" / "App.tsx").write_text(
        "export function App() { return <div>Counter</div>; }\n",
        encoding="utf-8",
    )
    (app / "src" / "react-app.d.ts").write_text(
        "declare module 'react' { export = React; }\n",
        encoding="utf-8",
    )
    (app / "e2e" / "home.spec.ts").write_text(
        'test("placeholder passing test", async () => { expect(true).toBeTruthy(); });\n',
        encoding="utf-8",
    )
    (app / "postcss.config.js").write_text(
        "export default { plugins: { tailwindcss: {}, autoprefixer: {} } };\n",
        encoding="utf-8",
    )
    (app / "itty-router.js").write_text(
        "export const Router = () => ({ all() { return this; }, fetch() {} });\n",
        encoding="utf-8",
    )
    (app / "server").mkdir()
    (app / "server" / "index.js").write_text(
        "import { Router } from 'itty-router';\n",
        encoding="utf-8",
    )
    (app / "playwright.config.js").write_text(
        'module.exports = { webServer: { command: "npm start" } };\n',
        encoding="utf-8",
    )
    (app / "package.json").write_text(
        '{"name":"hookwatch","scripts":{"verify":"npm run build && node server/index.js & sleep 1 && kill $!"}}',
        encoding="utf-8",
    )
    return app


def test_restore_official_hookwatch_replaces_counter_and_placeholder(tmp_path: Path):
    app = _stub_tree(tmp_path)
    assert hookwatch_is_stubbed(app) is True
    result = restore_official_hookwatch(app)
    assert result["restored"] is True
    assert (app / "src" / "App.tsx").read_text(encoding="utf-8").count("HookWatch") >= 1
    assert "inbox" in (app / "src" / "App.tsx").read_text(encoding="utf-8").lower()
    assert not (app / "src" / "react-app.d.ts").exists()
    assert not (app / "e2e" / "home.spec.ts").exists()
    assert not (app / "postcss.config.js").exists()
    assert not (app / "itty-router.js").exists()
    assert not (app / "server" / "index.js").exists()
    assert not (app / "playwright.config.js").exists()
    assert (app / "index.html").is_file()
    playwright = (app / "playwright.config.ts").read_text(encoding="utf-8")
    assert "node server.mjs" in playwright
    assert "npm run build &&" not in playwright
    e2e = (app / "e2e" / "inbox.spec.ts").read_text(encoding="utf-8")
    assert "expect(true)" not in e2e
    assert "create inbox" in e2e.lower()
    assert hookwatch_is_stubbed(app) is False
    again = restore_official_hookwatch(app)
    assert again["restored"] is True
    assert "Create inbox" in (app / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "hookwatch_active" in (app / "src" / "App.tsx").read_text(encoding="utf-8")


def test_restore_from_repo_root_writes_into_hookwatch_subdir(tmp_path: Path):
    app = _stub_tree(tmp_path)
    (tmp_path / "package.json").write_text('{"name":"sandbox"}', encoding="utf-8")
    result = restore_official_canary(tmp_path, app_id="hookwatch")
    assert result["restored"] is True
    assert "Create inbox" in (app / "src" / "App.tsx").read_text(encoding="utf-8")
    assert not (tmp_path / "src" / "App.tsx").exists()


def test_restore_finds_hookwatch_by_directory_when_app_id_missing(tmp_path: Path):
    app = _stub_tree(tmp_path)
    result = restore_official_canary(app, app_id=None)
    assert result["restored"] is True
    assert "Create inbox" in (app / "src" / "App.tsx").read_text(encoding="utf-8")


def test_prepare_restores_official_hookwatch_and_unstubs_verify(tmp_path: Path):
    app = _stub_tree(tmp_path)
    prep = prepare_web_verify_env(app, app_id="hookwatch", scenarios=["Create an inbox"])
    assert prep["restored"]["restored"] is True
    import json

    payload = json.loads((app / "package.json").read_text(encoding="utf-8"))
    scripts = payload["scripts"]
    assert "sleep" not in scripts["verify"]
    assert "typecheck" in scripts["verify"]
    assert payload.get("type") == "module"


def test_restore_prefers_verify_cwd_over_nested_leftover(tmp_path: Path):
    app = _stub_tree(tmp_path)
    nested = app / "hookwatch"
    nested.mkdir()
    (nested / "package.json").write_text('{"name":"nested-leftover"}', encoding="utf-8")
    (nested / "src").mkdir()
    (nested / "src" / "App.tsx").write_text(
        "export function App() { return <div>nested leftover</div>; }\n",
        encoding="utf-8",
    )
    result = restore_official_canary(app, app_id="hookwatch")
    assert result["restored"] is True
    assert "Create inbox" in (app / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "nested leftover" in (nested / "src" / "App.tsx").read_text(encoding="utf-8")
    assert not (app / "itty-router.js").exists()


def test_maybe_restore_for_command_rewrites_hookwatch_before_npm(tmp_path: Path):
    app = _stub_tree(tmp_path)
    note = maybe_restore_for_command(app, "npm run verify")
    assert note.startswith("[boss] sandbox.restore")
    assert "Create inbox" in (app / "src" / "App.tsx").read_text(encoding="utf-8")
    assert not (app / "playwright.config.js").exists()
    assert "node server.mjs" in (app / "playwright.config.ts").read_text(encoding="utf-8")
    assert maybe_restore_for_command(app, "python3 -m unittest") == ""


def test_restore_from_monorepo_root_does_not_write_sandbox_root(tmp_path: Path):
    app = _stub_tree(tmp_path)
    (tmp_path / "package.json").write_text('{"name":"boss-sandbox"}', encoding="utf-8")
    result = restore_official_canary(tmp_path, app_id="hookwatch")
    assert result["restored"] is True
    assert "Create inbox" in (app / "src" / "App.tsx").read_text(encoding="utf-8")
    assert not (tmp_path / "server.mjs").exists()
    assert not (tmp_path / "src" / "App.tsx").exists()
