"""The Community image installs every package the engine declares it needs.

Dockerfile.community installs apps/api/requirements.txt and apps/worker/requirements.txt, never
engine/requirements.txt. tree-sitter and its grammars were added to the engine's manifest only, so
the shipped image had none: the code graph parsed every Java, Go, C#, JavaScript and TypeScript
file with the fallback declaration scan (no cross-file calls), while every test that ran from a
developer's environment, which had them, passed. An engine dependency the image does not install
fails here, by name.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
#: Engine packages the image deliberately leaves out, each with its reason. Empty: none today.
NOT_IN_THE_IMAGE: dict[str, str] = {}


def _pins(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r"([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*(?:==\s*([^\s;]+))?", line)
        if match:
            out[match.group(1).lower().replace("_", "-")] = match.group(2) or ""
    return out


def test_the_image_manifests_cover_the_engine_manifest():
    dockerfile = (ROOT / "Dockerfile.community").read_text()
    manifests = re.findall(r"COPY (apps/\w+/requirements\.txt) ", dockerfile)
    assert manifests, "Dockerfile.community no longer names the requirement files it installs"
    image: dict[str, str] = {}
    for manifest in manifests:
        image.update(_pins(ROOT / manifest))
    engine = _pins(ROOT / "engine" / "requirements.txt")
    missing = sorted(set(engine) - set(image) - set(NOT_IN_THE_IMAGE))
    assert not missing, f"the image does not install these engine dependencies: {missing}"
    drift = sorted(f"{name}: engine {engine[name]} vs image {image[name]}" for name in set(engine) & set(image)
                   if engine[name] and image[name] and engine[name] != image[name])
    assert not drift, drift


def test_the_code_parsers_import_where_the_tests_run():
    """Where the engine's tests run, the grammars are real: a Java file gets tree-sitter facts."""
    from boss_engine.services.knowledge.code_index import parser_backend

    assert {parser_backend(lang) for lang in ("java", "go", "csharp", "javascript", "typescript")} == {"tree_sitter"}
