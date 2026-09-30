"""APP-1 (#91) — framework-aware scaffold service: deterministic, buildable baseline with a
design-system token layer and the web verification scripts APP-3 consumes."""
import json

from apps.api.app.services import scaffold_service as sc


def test_scaffold_is_deterministic():
    a = sc.scaffold_project("fitdefence", "vite")
    b = sc.scaffold_project("fitdefence", "vite")
    assert a == b                                   # same input -> identical tree (no model tokens)


def test_required_files_and_structure_present():
    files = sc.scaffold_project("myapp")["files"]
    for req in ("package.json", "tsconfig.json", "vite.config.ts", "tailwind.config.ts",
                "src/index.css", "src/main.tsx", "src/App.tsx", "src/lib/utils.ts",
                "src/components/ui/button.tsx", "src/components/ui/card.tsx",
                "components.json", "e2e/home.spec.ts"):
        assert req in files, f"missing {req}"


def test_package_json_has_web_verification_scripts():
    pkg = json.loads(sc.scaffold_project("myapp")["files"]["package.json"])
    for script in ("build", "typecheck", "lint", "test:e2e"):        # APP-3 consumes these
        assert script in pkg["scripts"], f"missing script {script}"
    assert "react" in pkg["dependencies"] and "tailwindcss" in pkg["devDependencies"]
    assert pkg["name"] == "myapp"


def test_design_system_token_layer_is_present():
    css = sc.scaffold_project("myapp")["files"]["src/index.css"]
    # one accent, radius + shadow, and a dark theme — the coherence source of truth
    for token in ("--accent:", "--radius:", "--shadow-md:", '[data-theme="dark"]', "@tailwind base"):
        assert token in css
    tw = sc.scaffold_project("myapp")["files"]["tailwind.config.ts"]
    assert "fontSize:" in tw and "accent:" in tw     # one type scale + token bridge


def test_starter_page_uses_kit_and_flags_placeholder_copy():
    app = sc.scaffold_project("myapp")["files"]["src/App.tsx"]
    assert "components/ui/button" in app and "components/ui/card" in app   # composes the kit
    assert "APP-2" in app                                                  # copy TODO for the design role


def test_json_files_parse():
    files = sc.scaffold_project("myapp")["files"]
    for f in ("package.json", "tsconfig.json", "components.json", ".eslintrc.json"):
        json.loads(files[f])                          # valid JSON -> buildable config


def test_manifest_describes_scaffold():
    m = sc.scaffold_project("myapp", "spa")["manifest"]
    assert m["kind"] == "vite" and m["design_tokens_file"] == "src/index.css"
    assert set(("build", "typecheck", "lint", "test:e2e")) <= set(m["scripts"])
    assert len(m["component_kit"]) >= 2


def test_kind_aliases_and_unsupported():
    assert sc.scaffold_project("x", "spa")["manifest"]["kind"] == "vite"
    assert sc.scaffold_project("x", "react")["manifest"]["kind"] == "vite"
    try:
        sc.scaffold_project("x", "nextjs")
        assert False, "nextjs should raise (follow-on)"
    except ValueError:
        pass


def test_name_is_sanitized():
    m = sc.scaffold_project("Fit Defence AI!", "vite")["manifest"]
    assert m["name"] == "fit-defence-ai-" or m["name"].startswith("fit-defence-ai")


def test_endpoint(client):
    resp = client.post("/v1/scaffold", json={"name": "fitdefence", "kind": "vite"})
    assert resp.status_code == 200
    body = resp.json()
    assert "package.json" in body["files"] and body["manifest"]["name"] == "fitdefence"
    assert client.post("/v1/scaffold", json={"kind": "nextjs"}).status_code == 422
