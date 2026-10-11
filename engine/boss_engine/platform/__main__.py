"""`python -m boss_engine.platform <command>`: the plugin SDK's command line.
(The same commands run as `boss-engine plugin <command>`.)

    init <dir> [--id ID]     scaffold a plugin (boss-plugin.yaml + plugin.py) that validates
    validate <dir>           check boss-plugin.yaml and that every declared hook is implemented
    test <dir> [--json]      run the contract harness against real ephemeral containers
         [--keep]
    checksum <dir>           the tree checksum a verified pin records
    matrix [--write]         print (or rewrite) docs/community/support-matrix.md
    schemas                  regenerate the committed boss.plugin/v1 JSON Schemas
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MANIFEST_TEMPLATE = """\
apiVersion: boss.plugin/v1
kind: Plugin
metadata:
  id: {id}
  name: {name}
  version: 0.1.0
  owner: you
  license: Apache-2.0
  description: What this plugin provides, in one line.
spec:
  supportLevel: community
  capabilities: [{capability}]
  features: []
  engines: []
  runtime:
    type: python
    entrypoint: plugin:Plugin
  permissions:
    secrets: [write-scoped]
    docker: true
    network: [declared-services]
  hooks: [validate, plan, provision, verify, destroy]
  bindings: [{binding}]
  ports: []
  limitations: []
"""

PLUGIN_TEMPLATE = '''\
"""{id}: see docs/community/plugin-authoring.md and plugins/community/example/plugin.py."""
from __future__ import annotations

from boss_engine.platform.contract import Evidence, HookContext, InfraPlugin, OperationResult, ResourcePlan


class Plugin(InfraPlugin):

    def validate(self, ctx: HookContext) -> OperationResult:
        return ctx.result()

    def plan(self, ctx: HookContext) -> OperationResult:
        return ctx.result(plan=ResourcePlan(
            resourceId=ctx.resource_id, pluginId=ctx.plugin_id, pluginVersion=ctx.plugin_version,
            mode=ctx.resource.mode, publicAccess=ctx.resource.public,
            bindings=[ctx.resource.bindings.get("{binding}", "{binding}")]))

    def provision(self, ctx: HookContext) -> OperationResult:
        # Start the resource with ctx.host.run(...), labelled with sdk.docker_labels_args(ctx).
        # Hand the connection value to ctx.secrets.put("{binding}", value); return only the ref.
        return ctx.fail("not_implemented", "provision is not written yet")

    def verify(self, ctx: HookContext) -> OperationResult:
        # Open a REAL connection and report it as Evidence(kind="connection").
        return ctx.result("failed", evidence=[Evidence(check="connect", passed=False, kind="connection")])

    def destroy(self, ctx: HookContext) -> OperationResult:
        return ctx.result()
'''


def cmd_init(directory: str, plugin_id: str | None = None, capability: str = "cache") -> int:
    d = Path(directory)
    if (d / "boss-plugin.yaml").exists():
        print(f"{d}/boss-plugin.yaml already exists", file=sys.stderr)
        return 1
    name = re.sub(r"[^a-z0-9-]+", "-", d.resolve().name.lower()).strip("-") or "plugin"
    plugin_id = plugin_id or f"community.{name}"
    binding = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") + "_URL"
    if not binding[0].isalpha():
        binding = "RESOURCE_URL"
    d.mkdir(parents=True, exist_ok=True)
    (d / "boss-plugin.yaml").write_text(MANIFEST_TEMPLATE.format(
        id=plugin_id, name=name, capability=capability, binding=binding), encoding="utf-8")
    (d / "plugin.py").write_text(PLUGIN_TEMPLATE.format(id=plugin_id, binding=binding), encoding="utf-8")
    (d / "harness.yaml").write_text(f"resource:\n  capability: {capability}\n", encoding="utf-8")
    print(f"created {d}/boss-plugin.yaml, plugin.py, harness.yaml — next: validate, then test")
    return 0


def cmd_validate(directory: str) -> int:
    from .contract import ContractError, PluginSource, load_manifest
    from .registry import RegisteredPlugin, RegistryError, load_plugin, tree_checksum
    d = Path(directory)
    try:
        manifest = load_manifest(d)
    except (ContractError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        load_plugin(RegisteredPlugin(manifest, PluginSource.COMMUNITY, d.resolve(), tree_checksum(d)))
    except (RegistryError, Exception) as exc:  # an import error in the plugin is a failed validate
        print(f"{manifest.id}: {exc}", file=sys.stderr)
        return 1
    print(f"{manifest.id} {manifest.version}: valid ({', '.join(h.value for h in manifest.spec.hooks)})")
    return 0


def cmd_test(directory: str, as_json: bool = False, keep: bool = False) -> int:
    from .harness import report_json, run_contract
    report = run_contract(directory, keep=keep)
    print(report_json(report) if as_json else report.text())
    return 0 if report.ok else 1


def cmd_checksum(directory: str) -> int:
    from .registry import tree_checksum
    print(tree_checksum(Path(directory)))
    return 0


def cmd_matrix(write: bool = False) -> int:
    from .docs import render_support_matrix, support_matrix_path
    text = render_support_matrix()
    if write:
        support_matrix_path().write_text(text, encoding="utf-8")
        print(support_matrix_path())
    else:
        print(text, end="")
    return 0


def cmd_schemas() -> int:
    from .contract import write_schemas
    for p in write_schemas():
        print(p)
    return 0


def add_commands(sub: "argparse._SubParsersAction") -> None:
    """The plugin commands, shared by this module and `boss-engine plugin`."""
    i = sub.add_parser("init", help="scaffold a plugin")
    i.add_argument("dir")
    i.add_argument("--id", dest="plugin_id")
    i.add_argument("--capability", default="cache")
    i.set_defaults(run=lambda a: cmd_init(a.dir, a.plugin_id, a.capability))
    v = sub.add_parser("validate", help="validate a plugin's manifest and hooks")
    v.add_argument("dir")
    v.set_defaults(run=lambda a: cmd_validate(a.dir))
    t = sub.add_parser("test", help="run the contract harness")
    t.add_argument("dir")
    t.add_argument("--json", action="store_true")
    t.add_argument("--keep", action="store_true", help="leave containers for debugging")
    t.set_defaults(run=lambda a: cmd_test(a.dir, a.json, a.keep))
    c = sub.add_parser("checksum", help="tree checksum of a plugin directory")
    c.add_argument("dir")
    c.set_defaults(run=lambda a: cmd_checksum(a.dir))
    m = sub.add_parser("matrix", help="the generated support matrix")
    m.add_argument("--write", action="store_true")
    m.set_defaults(run=lambda a: cmd_matrix(a.write))
    s = sub.add_parser("schemas", help="regenerate the JSON Schemas")
    s.set_defaults(run=lambda a: cmd_schemas())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m boss_engine.platform", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    add_commands(p.add_subparsers(dest="cmd", required=True))
    args = p.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
