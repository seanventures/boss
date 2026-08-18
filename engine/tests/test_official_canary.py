from pathlib import Path

from boss_engine.services.official_canary import (
    hookwatch_is_stubbed,
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
    e2e = (app / "e2e" / "inbox.spec.ts").read_text(encoding="utf-8")
    assert "expect(true)" not in e2e
    assert "create inbox" in e2e.lower()
    assert hookwatch_is_stubbed(app) is False
    again = restore_official_hookwatch(app)
    assert again["restored"] is True
    assert "Create inbox" in (app / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "hookwatch_active" in (app / "src" / "App.tsx").read_text(encoding="utf-8")


def test_prepare_restores_official_hookwatch_and_unstubs_verify(tmp_path: Path):
    app = _stub_tree(tmp_path)
    prep = prepare_web_verify_env(app, app_id="hookwatch", scenarios=["Create an inbox"])
    assert prep["restored"]["restored"] is True
    import json

    scripts = json.loads((app / "package.json").read_text(encoding="utf-8"))["scripts"]
    assert "sleep" not in scripts["verify"]
    assert "typecheck" in scripts["verify"]
