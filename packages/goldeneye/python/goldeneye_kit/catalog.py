"""Catalog assembly from modules/*/package.yaml."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

KIT_API_VERSION = 1
DEFAULT_BOOTSTRAP = [
    "layer0-host",
    "layer1-runtime",
    "layer2-edge",
    "layer3-control",
    "deploy-cli",
]


def _read_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        # minimal scalar parser
        out: dict[str, Any] = {}
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or ":" not in s:
                continue
            k, _, v = s.partition(":")
            key = k.strip()
            val = v.strip().strip('"').strip("'")
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                out[key] = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
            elif val.lower() in {"true", "false"}:
                out[key] = val.lower() == "true"
            else:
                try:
                    out[key] = int(val)
                except ValueError:
                    out[key] = val
        return out


def kit_root_from(start: Optional[Path] = None) -> Path:
    """Locate kit root (directory with VERSION and modules/)."""
    candidates: list[Path] = []
    if start is not None:
        candidates.append(start)
    here = Path(__file__).resolve()
    # python/shared_server_kit → python → kit
    candidates.extend(
        [
            here.parents[2],
            Path.cwd() / "packages" / "shared-server",
            Path.cwd(),
            Path("/opt/shared-server"),
        ]
    )
    for cand in candidates:
        if (cand / "VERSION").is_file() or (cand / "modules").is_dir():
            return cand
    return candidates[0]


def kit_version(root: Optional[Path] = None) -> str:
    root = root or kit_root_from()
    vf = root / "VERSION"
    if vf.is_file():
        return vf.read_text(encoding="utf-8").strip()
    return "0.0.0"


def discover_modules(root: Optional[Path] = None) -> list[Path]:
    root = root or kit_root_from()
    mod_root = root / "modules"
    if not mod_root.is_dir():
        # back-compat
        mod_root = root / "packages"
    if not mod_root.is_dir():
        return []
    out: list[Path] = []
    for d in sorted(mod_root.iterdir()):
        if not d.is_dir():
            continue
        if (d / "package.yaml").is_file() or (d / "install.sh").is_file():
            out.append(d)
    return out


def load_module_manifest(module_dir: Path) -> dict[str, Any]:
    mf = module_dir / "package.yaml"
    if mf.is_file():
        data = _read_yaml(mf)
    else:
        data = {
            "api_version": 1,
            "id": module_dir.name,
            "name": module_dir.name,
            "version": "0.0.0",
            "layer": 9,
            "kind": "optional",
            "depends_on": [],
            "optional": True,
        }
    mid = str(data.get("id") or module_dir.name)
    layer_raw = data.get("layer")
    layer = int(layer_raw) if layer_raw is not None and str(layer_raw) != "" else 9
    api_raw = data.get("api_version")
    api_version = int(api_raw) if api_raw is not None and str(api_raw) != "" else 1
    return {
        "api_version": api_version,
        "id": mid,
        "name": str(data.get("name") or mid),
        "version": str(data.get("version") or "0.0.0"),
        "layer": layer,
        "kind": str(data.get("kind") or "optional"),
        "description": str(data.get("description") or "").strip(),
        "depends_on": list(data.get("depends_on") or []),
        "optional": bool(data.get("optional", False)),
        "provides": list(data.get("provides") or []),
        "conflicts": list(data.get("conflicts") or []),
        "min_kit_version": data.get("min_kit_version"),
        "hooks": data.get("hooks") if isinstance(data.get("hooks"), dict) else {},
        "recipe": f"modules/{module_dir.name}",
        "path": f"modules/{module_dir.name}",
        "module_dir": str(module_dir),
    }


def build_catalog(root: Optional[Path] = None) -> dict[str, Any]:
    root = root or kit_root_from()
    packages = [load_module_manifest(d) for d in discover_modules(root)]
    # strip non-serializable absolute paths for catalog file consumers
    public = []
    for p in packages:
        item = {k: v for k, v in p.items() if k != "module_dir"}
        public.append(item)
    return {
        "version": 2,
        "api_version": KIT_API_VERSION,
        "kit_version": kit_version(root),
        "source": "modules/*/package.yaml",
        "default_bootstrap": list(DEFAULT_BOOTSTRAP),
        "packages": public,
        "package_count": len(public),
    }


def load_catalog(root: Optional[Path] = None, *, rebuild: bool = False) -> dict[str, Any]:
    """Load catalog.yaml or assemble from modules."""
    root = root or kit_root_from()
    cat = root / "catalog.yaml"
    if rebuild or not cat.is_file():
        return build_catalog(root)
    data = _read_yaml(cat)
    if not data.get("packages"):
        return build_catalog(root)
    # normalize version fields
    data.setdefault("api_version", KIT_API_VERSION)
    data.setdefault("kit_version", kit_version(root))
    data.setdefault("default_bootstrap", DEFAULT_BOOTSTRAP)
    data["package_count"] = len(data.get("packages") or [])
    return data


def write_catalog(root: Optional[Path] = None, out: Optional[Path] = None) -> Path:
    root = root or kit_root_from()
    out = out or (root / "catalog.yaml")
    catalog = build_catalog(root)
    try:
        import yaml  # type: ignore

        body = yaml.safe_dump(catalog, sort_keys=False, default_flow_style=False)
    except Exception:
        body = json.dumps(catalog, indent=2)
    header = (
        "# GENERATED by shared_server_kit / `ge catalog build`.\n"
        "# Source of truth: modules/*/package.yaml — do not hand-edit.\n"
    )
    out.write_text(header + body, encoding="utf-8")
    return out
