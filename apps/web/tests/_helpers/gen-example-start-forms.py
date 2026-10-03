"""Regenerate example-start-forms.json: what each examples/*/setup.py publishes as its start form.

Runs each setup script's main() with the API calls stubbed and captures the spec it would
publish. Usage, from the repo root:

    python3 apps/web/tests/_helpers/gen-example-start-forms.py "$PWD" \
        > apps/web/tests/_helpers/example-start-forms.json

tests/the-start-form-is-the-workflows-own.test.tsx checks the JSON still matches the scripts.
"""
import importlib.util, json, sys, types, collections
root = sys.argv[1]
sys.path.insert(0, f"{root}/examples")
out = {}
for name in ["trend-to-content", "sales-signal", "weekly-newsletter", "meeting-to-tickets", "video-workflow"]:
    spec_ = importlib.util.spec_from_file_location(name.replace("-", "_"), f"{root}/examples/{name}/setup.py")
    mod = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(mod)
    captured = {}
    class Keys(dict):
        def __missing__(self, k): return f"step-{k}"
    noop = lambda *a, **k: None
    for fn in ["enable_skills", "grant", "ensure_agents", "report_readiness", "connect"]:
        if hasattr(mod, fn): setattr(mod, fn, noop)
    mod.define_steps = lambda *a, **k: Keys()
    def publish(api, *, name, description, spec, args):
        captured.update(name=name, description=description, spec=spec); return "wf"
    mod.publish = publish
    sys.argv = ["setup.py"]
    import builtins; p = builtins.print; builtins.print = noop
    try: mod.main()
    finally: builtins.print = p
    spec = captured["spec"]
    out[name] = {"name": captured["name"], "description": captured["description"],
                 "input_schema": spec["input_schema"],
                 "steps": [{"id": s["id"], "kind": s.get("kind"),
                            "capability": (s.get("binding") or {}).get("capability")} for s in spec["steps"]]}
print(json.dumps(out, indent=2, ensure_ascii=False))
