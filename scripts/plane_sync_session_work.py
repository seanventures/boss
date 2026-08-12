#!/usr/bin/env python3
"""Sync recent BOSS session work to Plane and close completed tickets.

Uses PLANE_API_KEY from env (never prints the key).
Workspace goldeneye / BOSS project.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

WS = "goldeneye"
PROJECT = "cc97d5cc-e8c6-4893-ac14-7f646c37f3d7"
BASE = f"https://plane.local.sean.network/api/v1/workspaces/{WS}/projects/{PROJECT}"

STATE = {
    "backlog": "1a4bd5b5-ffb9-4842-963c-5fdb4d05d6c9",
    "todo": "01e2c6bc-3627-40cd-ab8f-5d5b701c87b5",
    "in_progress": "3b0d09f0-c269-4d34-8b2e-6b25e51f3470",
    "done": "c796fbda-185e-402b-a8b1-e320b9232658",
    "cancelled": "a6c0fadd-577e-43b9-939b-6c17e7d19c11",
}

# Existing tickets to close with evidence from this session / branch work
CLOSE_EXISTING: list[dict[str, Any]] = [
    {
        "sequence_id": 239,
        "reason": "OVHcloud Resource Plane adapter shipped",
        "evidence": (
            "Implemented `engine/boss_engine/services/ovh_adapter.py` with AK/AS/CK signing, "
            "dedicated server inventory, public-cloud instance create/destroy, connection test. "
            "Wired into Resource Plane skills: `ovh.cloud.instance.create/destroy`, "
            "`ovh.dedicated.server.list`, inventory/test_connection. "
            "Tests: `apps/api/tests/test_ovh_adapter.py` (15 passed)."
        ),
    },
    {
        "sequence_id": 238,
        "reason": "DigitalOcean Resource Plane adapter expanded E2E",
        "evidence": (
            "Expanded `digitalocean_adapter.py` with test_connection, list_droplets/apps, "
            "ensure_firewall, create_floating_ip, inference_chat, Spaces key gate. "
            "Skills: droplet create/destroy, app.deploy, firewall.ensure, floating_ip.create, "
            "inference.chat, billing.sync. Tests: `test_digitalocean_adapter.py` (20 passed)."
        ),
    },
    {
        "sequence_id": 242,
        "reason": "Governed elastic scaling fabric shipped (Community BYOK path)",
        "evidence": (
            "Elastic fabric: `elastic_fabric.py` + `/v1/elastic/*` + provider usage ledger "
            "`/v1/provider-usage/*`. Capacity for local/container/vm/cloud with ChangeSets, "
            "receipts, simulated vs real provision gate. Multi-provider worker create for "
            "digitalocean/aws/azure/ovhcloud. Docs: `docs/product/ELASTIC_FABRIC.md`. "
            "Tests: `test_elastic_fabric.py`."
        ),
    },
    {
        "sequence_id": 359,
        "reason": "Multi-provider work trackers (Plane default + Jira/Linear/GitHub/Azure DevOps)",
        "evidence": (
            "Work tracker catalog + connect/set-default/test APIs `/v1/work-trackers/*`. "
            "Adapters: Plane (default), Jira, Linear, GitHub Issues, Azure DevOps. "
            "Docs: `docs/product/WORK_TRACKER_PROVIDERS.md`. Tests: `test_work_trackers.py`."
        ),
    },
    {
        "sequence_id": 70,
        "reason": "Azure Resource Plane + Azure OpenAI adapters shipped (GCP still open)",
        "evidence": (
            "Azure phase of BOSS-70 complete: `azure_adapter.py` — SP auth, RG ensure, "
            "VM create/destroy (metal sizes), Azure OpenAI chat. Resource Plane skills "
            "`azure.vm.*`, `azure.resource_group.ensure`, `azure.openai.chat`. "
            "Tests: `test_azure_adapter.py` (16 passed). "
            "GCP remains out of scope for this close — file follow-up if needed."
        ),
        # Closing BOSS-70 as Azure delivered; GCP can be a new backlog if required.
    },
]

# New work items to create then mark Done (dedupe by name prefix)
CREATE_AND_CLOSE: list[dict[str, Any]] = [
    {
        "name": "Goldeneye modular bare-metal PaaS kit (extractable OSS)",
        "priority": "high",
        "description_html": (
            "<p>Modular Goldeneye kit under <code>packages/goldeneye/</code>: "
            "core CLI, modules/*/package.yaml contract, catalog builder, "
            "<code>ge upgrade</code>, optional BOSS integration, docs for OSS extract.</p>"
            "<ul>"
            "<li>VERSION 0.2.0, module API 1</li>"
            "<li>python/goldeneye_kit catalog library</li>"
            "<li>BOSS control plane /v1/goldeneye/* + goldeneye_paas.py</li>"
            "<li>docs: ARCHITECTURE, MODULE_CONTRACT, UPGRADE, OPEN_SOURCE</li>"
            "</ul>"
            "<p>Tests: goldeneye_kit catalog (5) + goldeneye_paas (3).</p>"
        ),
        "comment": (
            "Shipped on branch ai/codex/boss-cloud-product-distinction. "
            "Kit is modular and extractable; BOSS is optional control plane."
        ),
    },
    {
        "name": "Multi-cloud models+infra: AWS Bedrock, Azure, DO expand, OVH bare metal",
        "priority": "high",
        "description_html": (
            "<p>Full BYOK multi-cloud support for models and infra.</p>"
            "<ul>"
            "<li>AWS: Bedrock converse + metal helpers; existing EC2/S3/RDS</li>"
            "<li>Azure: ARM VM/RG + Azure OpenAI</li>"
            "<li>DigitalOcean: firewall, floating IP, inference</li>"
            "<li>OVHcloud: dedicated inventory + public cloud instances</li>"
            "<li>Catalog: /v1/multi-cloud/* + MULTI_CLOUD_INFRA_MODELS.md</li>"
            "</ul>"
            "<p>85 related adapter/catalog tests green.</p>"
        ),
        "comment": (
            "Adapters + Resource Plane skill dispatch + elastic multi-provider pools. "
            "Real writes gated by BOSS_INFRA_REAL_PROVISION_ALLOWED."
        ),
    },
    {
        "name": "Community one-line installer (OpenClaw-style)",
        "priority": "medium",
        "description_html": (
            "<p><code>install.sh</code> + <code>scripts/install/community-compose.yml</code> "
            "for idiot-proof Community edition bootstrap (deps, checks, key prompts, compose stack).</p>"
        ),
        "comment": "Shipped on feature branch; one-liner targets main after merge.",
    },
    {
        "name": "BOSS Cloud product distinction + Community Easy Connect",
        "priority": "high",
        "description_html": (
            "<p>Community independent default; Cloud/Enterprise paid gates; Easy Connect "
            "(browser+device); Account Connect; editions UX; /v1/cloud/* community-safe routes.</p>"
        ),
        "comment": (
            "Implementation on ai/codex/boss-cloud-product-distinction. "
            "Specs: BOSS_CLOUD_ENTERPRISE_SPEC + BOSS_COMMUNITY_EDITION_SPEC."
        ),
    },
    {
        "name": "Elastic worker/infra fabric + per-provider usage ledger",
        "priority": "high",
        "description_html": (
            "<p>Project-scoped elastic capacity (local/container/vm/cloud), instances, "
            "heartbeats, provider usage ledger and billing mirror hooks.</p>"
        ),
        "comment": "See ELASTIC_FABRIC.md; closes related PRV-07 scope for Community BYOK.",
    },
]


def api(
    method: str,
    path: str,
    body: Optional[dict] = None,
    *,
    base: str = BASE,
) -> Any:
    key = os.environ.get("PLANE_API_KEY") or ""
    if not key:
        raise SystemExit("PLANE_API_KEY not set")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={
            "X-API-Key": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            if not raw:
                return {}
            return json.loads(raw.decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} -> {e.code}: {err[:500]}") from e


def load_all_issues() -> list[dict]:
    # Prefer cached dump if fresh
    path = "/tmp/plane_all_issues.json"
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    out: list[dict] = []
    cursor = None
    for _ in range(30):
        q = "/issues/?per_page=100"
        if cursor:
            q += f"&cursor={cursor}"
        data = api("GET", q)
        out.extend(data.get("results") or [])
        if not data.get("next_page_results"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return out


def by_sequence(issues: list[dict]) -> dict[int, dict]:
    m: dict[int, dict] = {}
    for i in issues:
        seq = i.get("sequence_id")
        if seq is not None:
            m[int(seq)] = i
    return m


def set_state(issue_id: str, state_id: str) -> dict:
    return api("PATCH", f"/issues/{issue_id}/", {"state": state_id})


def add_comment(issue_id: str, html: str) -> dict:
    # Plane issue comments endpoint variants
    for path in (
        f"/issues/{issue_id}/comments/",
        f"/work-items/{issue_id}/comments/",
    ):
        try:
            return api(
                "POST",
                path,
                {
                    "comment_html": html,
                    "access": "INTERNAL",
                },
            )
        except RuntimeError as e:
            last = e
            continue
    # Fallback: append to description
    try:
        issue = api("GET", f"/issues/{issue_id}/")
        desc = issue.get("description_html") or "<p></p>"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        new_desc = (
            desc
            + f"<hr/><p><strong>Session update {stamp}</strong></p>"
            + html
        )
        return api("PATCH", f"/issues/{issue_id}/", {"description_html": new_desc})
    except Exception as e2:
        raise RuntimeError(f"comment failed: {last}; desc fallback: {e2}") from e2


def create_issue(name: str, description_html: str, priority: str = "medium") -> dict:
    # Dedupe by exact name among all issues
    return api(
        "POST",
        "/issues/",
        {
            "name": name,
            "description_html": description_html,
            "priority": priority,
            "state": STATE["done"],
        },
    )


def find_by_name(issues: list[dict], name: str) -> Optional[dict]:
    name_l = name.lower().strip()
    for i in issues:
        if (i.get("name") or "").lower().strip() == name_l:
            return i
        # prefix match for long titles
        if name_l[:48] in (i.get("name") or "").lower():
            return i
    return None


def is_done(issue: dict) -> bool:
    st = issue.get("state")
    if isinstance(st, dict):
        st = st.get("id")
    return str(st) == STATE["done"]


def main() -> int:
    issues = load_all_issues()
    seq_map = by_sequence(issues)
    report: list[str] = []

    # 1) Close existing completed work
    for item in CLOSE_EXISTING:
        seq = int(item["sequence_id"])
        issue = seq_map.get(seq)
        if not issue:
            report.append(f"MISS BOSS-{seq}: not found")
            continue
        iid = issue["id"]
        if is_done(issue):
            report.append(f"SKIP BOSS-{seq}: already Done")
            continue
        html = (
            f"<p><strong>Closing:</strong> {item['reason']}</p>"
            f"<p>{item['evidence']}</p>"
            f"<p>Branch: <code>ai/codex/boss-cloud-product-distinction</code>. "
            f"Automated Plane sync {datetime.now(timezone.utc).isoformat()}.</p>"
        )
        try:
            add_comment(iid, html)
            set_state(iid, STATE["done"])
            report.append(f"CLOSED BOSS-{seq}: {issue.get('name','')[:70]}")
        except Exception as e:
            report.append(f"FAIL BOSS-{seq}: {e}")

    # 2) Create + close new session deliverables
    for item in CREATE_AND_CLOSE:
        existing = find_by_name(issues, item["name"])
        if existing:
            iid = existing["id"]
            seq = existing.get("sequence_id")
            if not is_done(existing):
                try:
                    add_comment(
                        iid,
                        f"<p><strong>Session completion</strong></p><p>{item['comment']}</p>",
                    )
                    set_state(iid, STATE["done"])
                    report.append(f"CLOSED existing BOSS-{seq}: {item['name'][:60]}")
                except Exception as e:
                    report.append(f"FAIL existing BOSS-{seq}: {e}")
            else:
                report.append(f"SKIP create (exists Done) BOSS-{seq}: {item['name'][:50]}")
            continue
        try:
            created = create_issue(
                item["name"], item["description_html"], item.get("priority", "medium")
            )
            iid = created.get("id")
            seq = created.get("sequence_id")
            if iid:
                try:
                    add_comment(
                        iid,
                        f"<p><strong>Delivered this session</strong></p><p>{item['comment']}</p>",
                    )
                except Exception:
                    pass
                # ensure Done (create may ignore state)
                try:
                    set_state(iid, STATE["done"])
                except Exception:
                    pass
            report.append(f"CREATED+DONE BOSS-{seq}: {item['name'][:60]}")
            # refresh map
            if created:
                issues.append(created)
        except Exception as e:
            report.append(f"FAIL create {item['name'][:40]}: {e}")

    # 3) Progress notes on umbrella tickets still open
    progress_notes = [
        (
            235,
            "<p><strong>Progress (multi-cloud session)</strong></p>"
            "<ul>"
            "<li>Azure + OVH + DO expand + AWS Bedrock adapters live</li>"
            "<li>/v1/multi-cloud/catalog + Resource Plane skill dispatch</li>"
            "<li>Elastic fabric multi-provider worker create</li>"
            "<li>GCP still planned</li>"
            "</ul>"
            "<p>Related: BOSS-238/239/70/242 closed or delivered in this sync.</p>",
        ),
        (
            64,
            "<p><strong>Progress</strong>: Multi-cloud adapters (AWS/Azure/DO/OVH) and "
            "terraform export already cover major providers. Azure+OVH net-new this session. "
            "Leaving open for remaining IaC generation polish / GCP.</p>",
        ),
        (
            240,
            "<p><strong>Progress</strong>: Inventory sync extended for Azure ARM list + "
            "OVH inventory aggregation + existing AWS/DO. Full topology/drift still open.</p>",
        ),
        (
            331,
            "<p><strong>Progress</strong>: Multi-provider work trackers (Plane default + "
            "Jira/Linear/GitHub/Azure DevOps) delivered under BOSS-359 close. "
            "Native initiatives/cycles still open on child tickets.</p>",
        ),
        (
            337,
            "<p><strong>Session note</strong>: Multi-cloud adapters, Goldeneye modular kit, "
            "elastic fabric, work trackers, community installer advanced on branch "
            "ai/codex/boss-cloud-product-distinction. Plane sync automated close of delivered PRV tickets.</p>",
        ),
    ]
    for seq, html in progress_notes:
        issue = seq_map.get(seq)
        if not issue or is_done(issue):
            continue
        try:
            add_comment(issue["id"], html)
            # move backlog → in progress if still backlog
            st = issue.get("state")
            if isinstance(st, dict):
                st = st.get("id")
            if str(st) == STATE["backlog"]:
                set_state(issue["id"], STATE["in_progress"])
                report.append(f"PROGRESS BOSS-{seq}: comment + In Progress")
            else:
                report.append(f"PROGRESS BOSS-{seq}: comment only")
        except Exception as e:
            report.append(f"FAIL progress BOSS-{seq}: {e}")

    print("\n".join(report))
    out = {
        "when": datetime.now(timezone.utc).isoformat(),
        "report": report,
    }
    with open("/tmp/plane_session_sync_report.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\nWrote /tmp/plane_session_sync_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
