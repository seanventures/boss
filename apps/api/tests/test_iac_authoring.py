"""IaC authoring in code agents (spec 05 §3.2 / T4, BOSS-68): hermetic tests.

Covers the four T4 pieces:
  1. context whitelist — .tf/.tfvars/.hcl (and .tf.json via the ".json" suffix) are
     readable repo context for the native agent;
  2. module-aware context packing — core Terraform files (main/variables/outputs/
     versions/providers .tf + *.tfvars) always survive the 40-blob and 20KB caps;
  3. task detection + lane routing — is_iac_task() and the run_code_task lane split:
     IaC tasks NEVER run on the truncating native inline lane, with auditable
     finalize_ctx evidence for why;
  4. infrastructure persona — Terraform-aware guidance in the qa/impl/solo prompts
     for IaC tasks only, with the QA stage reframed at module-structure assertions.

Plus the sample-module e2e: a clean AWS module fixture passes the full T3 verify
lane (fake-binary harness — no real terraform).
"""
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from apps.api.app.config import Settings
from apps.api.app.db import begin
from apps.api.app.services import code_task_service, governed_code_task_service
from apps.api.app.services.code_agent import NativeCodeAgent, _is_core_tf_file
from apps.api.app.services.core import make_id
from apps.api.app.services.governed_code_task_service import (
    _impl_prompt,
    _qa_prompt,
    _solo_prompt,
)
from apps.api.app.services.iac_verify import is_iac_task, run_iac_verify_lane


def _ctx(cwd, limit=40):
    # _repo_context uses no instance state — call it unbound (no LLM adapter needed).
    return NativeCodeAgent._repo_context(None, Path(cwd), limit=limit)


def _contents_section(ctx: str) -> str:
    # Everything after FILE CONTENTS: (the listing above names ALL files either way).
    return ctx.split("FILE CONTENTS:", 1)[1]


# --------------------------------------------------------------------------- #
#  1. Context whitelist                                                       #
# --------------------------------------------------------------------------- #
def test_context_whitelist_includes_tf_tfvars_hcl(tmp_path):
    (tmp_path / "net.tf").write_text('resource "aws_vpc" "main" {}\n')
    (tmp_path / "dev.tfvars").write_text('region = "us-east-1"\n')
    (tmp_path / "mod.hcl").write_text('locals { a = 1 }\n')
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / "skip.tfx").write_text("not terraform\n")
    body = _contents_section(_ctx(tmp_path))
    assert "--- net.tf ---" in body and 'resource "aws_vpc" "main"' in body
    assert "--- dev.tfvars ---" in body
    assert "--- mod.hcl ---" in body
    assert "--- app.py ---" in body
    assert "--- skip.tfx ---" not in body  # unknown extension stays excluded


def test_context_includes_tf_json_via_json_suffix(tmp_path):
    # Path("foo.tf.json").suffix == ".json", which is already whitelisted — prove it.
    (tmp_path / "foo.tf.json").write_text('{"resource": {"aws_s3_bucket": {"b": {}}}}\n')
    body = _contents_section(_ctx(tmp_path))
    assert "--- foo.tf.json ---" in body and "aws_s3_bucket" in body


# --------------------------------------------------------------------------- #
#  2. Module-aware packing: core Terraform files beat the caps                #
# --------------------------------------------------------------------------- #
def test_core_tf_files_survive_the_40_blob_cap(tmp_path):
    # 45 .py files sort BEFORE main.tf/… and would fill all 40 slots on their own.
    for i in range(45):
        (tmp_path / f"a{i:02}.py").write_text(f"# module {i}\n")
    big_main = "# HEAD-MARKER main.tf\n" + ('resource "null_resource" "x" {}\n' * 1200)
    assert len(big_main) >= 20000  # over the per-file cap too
    (tmp_path / "main.tf").write_text(big_main)
    (tmp_path / "variables.tf").write_text('variable "name" { type = string }\n')
    (tmp_path / "outputs.tf").write_text('output "id" { value = "x" }\n')
    (tmp_path / "versions.tf").write_text('terraform { required_version = ">= 1.6" }\n')
    (tmp_path / "prod.tfvars").write_text('name = "prod"\n')

    body = _contents_section(_ctx(tmp_path))
    for core in ("main.tf", "variables.tf", "outputs.tf", "versions.tf", "prod.tfvars"):
        assert f"--- {core} ---" in body, f"core file {core} must always be in context"
    # Oversized core file: truncated head with an explicit marker, not dropped.
    assert "# HEAD-MARKER main.tf" in body
    assert "[truncated: core Terraform file exceeds the 20KB context cap" in body
    # The overall cap still applies to non-core files (core displaced 5 slots).
    assert "--- a00.py ---" in body
    assert "--- a44.py ---" not in body
    assert body.count("--- ") == 40  # exactly 40 blobs total (cap held, core included)


def test_is_core_tf_file_names():
    for p in ("main.tf", "infra/variables.tf", "m/outputs.tf", "versions.tf",
              "mod/providers.tf", "envs/prod.tfvars"):
        assert _is_core_tf_file(p) is True
    for p in ("infra/vpc.tf", "app.py", "notes.tf.json", "main.tfx"):
        assert _is_core_tf_file(p) is False


# --------------------------------------------------------------------------- #
#  3a. is_iac_task detection                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec,title", [
    ({"task_type": "iac"}, "Anything at all"),
    ({"instructions": "Write a Terraform module provisioning an S3 bucket"}, "Storage"),
    ({}, "Migrate our stacks to OpenTofu"),
    ({"acceptance_criteria": ["module ships main.tf and variables.tf"]}, "Module work"),
    ({"instructions": "Adopt infrastructure as code for the VPC layer"}, "Networking"),
    ({"instructions": "Adopt Infrastructure-as-Code for the VPC layer"}, "Networking"),
    ({"instructions": "clean up the shared .hcl locals"}, "Refactor"),
])
def test_is_iac_task_positives(spec, title):
    assert is_iac_task(spec, title) is True


@pytest.mark.parametrize("spec,title", [
    ({"instructions": "Major platform reform of the login flow"}, "Platform reform"),
    ({"instructions": "Add a REST endpoint returning JSON"}, "Generic web task"),
    ({"acceptance_criteria": ["renders on mobile"], "task_type": "web"}, "Landing page"),
    ({"instructions": "rename notes.tfx exporter"}, "Cleanup"),  # .tfx is not .tf
    ({}, ""),
    (None, None),
])
def test_is_iac_task_negatives(spec, title):
    assert is_iac_task(spec, title) is False


# --------------------------------------------------------------------------- #
#  3b. Lane routing: IaC tasks never run on the native inline lane            #
# --------------------------------------------------------------------------- #
def _insert_running_run(connection, *, title, spec):
    project_id = connection.execute(
        text("SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar_one()
    run_id = make_id("crun")
    connection.execute(
        text("INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, "
             "spec, finalize_ctx) VALUES (:id, :p, NULL, 'running', 'queued', :t, "
             "CAST(:s AS JSONB), '{}'::jsonb)"),
        {"id": run_id, "p": project_id, "t": title, "s": json.dumps(spec)},
    )
    return run_id


def test_iac_task_routes_governed_even_with_pipeline_flag_off(engine, monkeypatch):
    """spec T4 acceptance: IaC tasks run on the governed lane, not the truncating
    native lane — even when BOSS_GOVERNED_CODING_PIPELINE is false."""
    sentinel = {"status": "governed-sentinel", "done": True}
    monkeypatch.setattr(governed_code_task_service, "run_governed_code_task",
                        lambda *a, **k: sentinel)
    with begin(engine) as c:
        run_id = _insert_running_run(
            c, title="Provision S3 via Terraform",
            spec={"instructions": "Author a terraform module for the app bucket"})
    settings = Settings(BOSS_ENABLE_CODE_TASK="true", BOSS_GOVERNED_CODING_PIPELINE="false")
    res = code_task_service.run_code_task(engine, settings, None, run_id)
    assert res is sentinel

    # Auditable routing evidence in finalize_ctx (mirrors execution_substrate).
    with begin(engine) as c:
        fctx = c.execute(
            text("SELECT finalize_ctx FROM code_task_runs WHERE id=:id"),
            {"id": run_id}).scalar_one()
    fctx = fctx if isinstance(fctx, dict) else json.loads(fctx)
    lr = fctx.get("lane_routing")
    assert lr and lr["lane"] == "governed" and lr["iac_task"] is True
    assert lr["forced"] is True  # flag was off; the IaC classification forced it
    assert "native lane" in lr["reason"]


def test_iac_task_type_field_alone_routes_governed(engine, monkeypatch):
    sentinel = {"status": "governed-sentinel", "done": True}
    monkeypatch.setattr(governed_code_task_service, "run_governed_code_task",
                        lambda *a, **k: sentinel)
    with begin(engine) as c:
        run_id = _insert_running_run(c, title="Provision the app bucket",
                                     spec={"task_type": "iac"})
    settings = Settings(BOSS_ENABLE_CODE_TASK="true", BOSS_GOVERNED_CODING_PIPELINE="false")
    assert code_task_service.run_code_task(engine, settings, None, run_id) is sentinel


class _NativeLaneMarker(Exception):
    """Raised by the patched native-lane entry point to prove which lane ran."""


def test_non_iac_task_stays_native_when_flag_off(engine, monkeypatch):
    monkeypatch.setattr(
        governed_code_task_service, "run_governed_code_task",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not go governed")))
    # First thing the native lane builds is the SoftwareFactoryService — mark it.
    monkeypatch.setattr(
        code_task_service, "SoftwareFactoryService",
        lambda *a, **k: (_ for _ in ()).throw(_NativeLaneMarker()))
    with begin(engine) as c:
        run_id = _insert_running_run(
            c, title="Add login endpoint",
            spec={"instructions": "Return the profile as JSON from the API"})
    settings = Settings(BOSS_ENABLE_CODE_TASK="true", BOSS_GOVERNED_CODING_PIPELINE="false")
    with pytest.raises(_NativeLaneMarker):
        code_task_service.run_code_task(engine, settings, None, run_id)
    # No lane_routing evidence is written for the default (non-forced) native path.
    with begin(engine) as c:
        fctx = c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id=:id"),
                         {"id": run_id}).scalar_one()
    fctx = fctx if isinstance(fctx, dict) else json.loads(fctx)
    assert "lane_routing" not in (fctx or {})


# --------------------------------------------------------------------------- #
#  4. Infrastructure persona in the stage prompts                             #
# --------------------------------------------------------------------------- #
_PERSONA_MARKERS = ("INFRASTRUCTURE ROLE", "required_providers", "sensitive = true",
                    "for_each", "-backend=false")


def test_impl_prompt_carries_infrastructure_persona_for_iac_only():
    iac = _impl_prompt("Provision bucket", "do it", ["test_mod.py"], "cmd", iac=True)
    for marker in _PERSONA_MARKERS:
        assert marker in iac
    plain = _impl_prompt("Provision bucket", "do it", ["test_mod.py"], "cmd")
    assert "INFRASTRUCTURE ROLE" not in plain and "required_providers" not in plain


def test_qa_prompt_reframes_tests_at_module_structure_for_iac():
    iac = _qa_prompt("Provision bucket", "do it", ["c1"], "cmd", iac=True)
    assert "INFRASTRUCTURE ROLE" in iac
    assert "MODULE STRUCTURE" in iac  # assertions target files/variables/outputs …
    assert "IaC verify lane" in iac   # … while terraform itself is verified by T3
    assert "unittest" in iac          # still the stdlib-unittest harness
    plain = _qa_prompt("Provision bucket", "do it", ["c1"], "cmd")
    assert "MODULE STRUCTURE" not in plain and "INFRASTRUCTURE ROLE" not in plain


def test_solo_prompt_gets_both_persona_and_qa_reframing_for_iac():
    iac = _solo_prompt("Provision bucket", "do it", ["c1"], "cmd", iac=True)
    assert "INFRASTRUCTURE ROLE" in iac and "MODULE STRUCTURE" in iac
    plain = _solo_prompt("Provision bucket", "do it", ["c1"], "cmd")
    assert "INFRASTRUCTURE ROLE" not in plain


# --------------------------------------------------------------------------- #
#  Sample-module e2e: a clean AWS module passes the full T3 verify lane       #
#  (fake-binary harness, mirrors test_iac_verify.py)                          #
# --------------------------------------------------------------------------- #
def _write_bin(bindir, name, body):
    p = bindir / name
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(0o755)


def _green_toolchain(bindir):
    _write_bin(bindir, "tofu", (
        'case "$1" in\n'
        '  fmt) echo "fmt checked"; exit 0 ;;\n'
        '  init) echo "Initializing the backend..."; exit 0 ;;\n'
        '  validate) echo \'{"format_version":"1.0","valid":true,"error_count":0}\'; exit 0 ;;\n'
        '  plan) echo "Plan: 2 to add, 0 to change, 0 to destroy."; exit 0 ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'))
    _write_bin(bindir, "tflint", "echo '{\"issues\":[]}'; exit 0")
    _write_bin(bindir, "checkov", "echo '{\"summary\":{\"failed\":0}}'; exit 0")


_SAMPLE_MODULE = {
    "versions.tf": (
        'terraform {\n'
        '  required_version = ">= 1.6"\n'
        '  required_providers {\n'
        '    aws = {\n'
        '      source  = "hashicorp/aws"\n'
        '      version = "~> 5.0"\n'
        '    }\n'
        '  }\n'
        '}\n'),
    "variables.tf": (
        'variable "bucket_names" {\n'
        '  type        = set(string)\n'
        '  description = "S3 bucket names to create."\n'
        '}\n\n'
        'variable "tags" {\n'
        '  type        = map(string)\n'
        '  description = "Tags applied to every bucket."\n'
        '  default     = {}\n'
        '}\n'),
    "main.tf": (
        'resource "aws_s3_bucket" "this" {\n'
        '  for_each = var.bucket_names\n\n'
        '  bucket = each.value\n'
        '  tags   = var.tags\n'
        '}\n'),
    "outputs.tf": (
        'output "bucket_arns" {\n'
        '  description = "ARNs of the created buckets."\n'
        '  value       = { for k, b in aws_s3_bucket.this : k => b.arn }\n'
        '}\n'),
}


def test_sample_aws_module_passes_full_verify_lane(tmp_path, monkeypatch):
    """T4 acceptance, local stand-in: a multi-file AWS Terraform module (main/
    variables/outputs/versions .tf) passes the T3 iac_verify lane end-to-end.

    The toolchain is the same fake-binary harness as test_iac_verify.py — this
    asserts the module fixture and lane wiring, not real terraform semantics. The
    live-LLM authoring e2e ("agent writes the module, real terraform verifies it")
    is deferred together with the live-AWS decision for the adapter work.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    _green_toolchain(bindir)

    module = tmp_path / "repo" / "infra"
    module.mkdir(parents=True)
    for name, content in _SAMPLE_MODULE.items():
        (module / name).write_text(content, encoding="utf-8")

    # Fixture hygiene: the "no hardcoded secrets" posture the persona demands.
    joined = "\n".join(_SAMPLE_MODULE.values())
    assert "AKIA" not in joined and "secret" not in joined.lower()
    assert "backend \"" not in joined  # backend config is operator-owned

    res = run_iac_verify_lane(
        tmp_path / "repo", Settings(),
        files={f"infra/{name}": content for name, content in _SAMPLE_MODULE.items()})
    assert res.ok is True
    assert res.tf_binary == "tofu"
    assert sorted(res.files) == ["infra/main.tf", "infra/outputs.tf",
                                 "infra/variables.tf", "infra/versions.tf"]
    non_skipped = [s for s in res.steps if not s.skipped]
    assert [s.step for s in non_skipped] == ["fmt", "validate", "lint", "security", "plan_dry"]
    assert all(s.ok for s in non_skipped)
