"""Load the AML case study's sample bank into a project — the product way, never a SQL script.

One action, offered by the console (Knowledge Graph › Documents › AML sample, POST
/v1/knowledge/samples/aml) and by asking BOSS (the `load_aml_sample` tool), both calling
`load_sample` here:

  1. sets the project's ontology pack to `banking-aml` (which includes banking-regulatory, so a
     project already running the regulatory case keeps its regulation kinds);
  2. registers the sample's three mock MCP connectors — case-management, kyc, transactions —
     served by the `mcp-tools` service (tools/mcp_aml_mock.py), first-party reviewed like the
     Service Desk mock;
  3. writes the bank's entities into the Knowledge Graph through the store's own primitives
     (`upsert_node`/`upsert_edge`, trust `observed`, as a system of record's data): customers,
     accounts, counterparties, KYC profiles, typologies, aggregated `transfers_to` flows,
     relationships on record, the three open alerts and the transactions they flag. The full
     ledger stays in the transactions connector;
  4. installs the catalogue into the project, so `aml_alert_investigation` (which needs the pack
     and the connectors) is there to start.

Idempotent: every write is an upsert keyed by the sample's own ids, so loading twice changes
nothing. The data is synthetic and deterministic (tools/aml_sample_data.py).
"""
from __future__ import annotations

import os
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()
PACK_ID = "banking-aml"
WORKFLOW_KEY = "aml_alert_investigation"
ACTOR_REF = "aml_sample"


def _aml_port() -> int:
    try:
        return int(os.environ.get("BOSS_MCP_AML_PORT", "").strip() or 8773)
    except ValueError:
        return 8773


def aml_mcp_servers() -> list[dict[str, Any]]:
    """The connectors the sample registers, at the `mcp-tools` service's address (the same host
    rule as bootstrap.BUILTIN_MCP_SERVERS)."""
    from ...bootstrap import MCP_TOOLS_HOST

    base = f"http://{MCP_TOOLS_HOST}:{_aml_port()}"
    return [
        {"name": "case-management", "url": f"{base}/case-management/mcp", "risk_tier": 1,
         "description": "AML sample case management: get_alert, list_alerts, add_case_note "
                        "(the only write; never closes an alert or files a report)"},
        {"name": "kyc", "url": f"{base}/kyc/mcp", "risk_tier": 0,
         "description": "AML sample KYC: get_kyc_profile"},
        {"name": "transactions", "url": f"{base}/transactions/mcp", "risk_tier": 0,
         "description": "AML sample transactions: query_transactions by account and date window"},
    ]


def node_key(kind: str, sample_id: str) -> str:
    return f"aml:{kind}:{sample_id}"


def node_id_of(connection: Connection, project_id: str, kind: str, sample_id: str) -> str:
    """The graph id of one of the sample's entities, or "" when the sample is not loaded."""
    row = connection.execute(
        text("SELECT id FROM kg_nodes WHERE project_id = :p AND node_key = :k "
             "AND (valid_to IS NULL OR valid_to > now()) ORDER BY recorded_at DESC LIMIT 1"),
        {"p": project_id, "k": node_key(kind, sample_id)}).scalar()
    return str(row or "")


def load_sample(connection: Connection, *, project_id: str, actor: str) -> dict[str, Any]:
    from tools.aml_sample_data import BANK, generate

    from .. import mcp_registry
    from ...bootstrap import builtin_provenance
    from . import ontology_packs

    ontology_packs.set_project_pack(connection, project_id=project_id, pack_id=PACK_ID,
                                    set_by=actor)

    # LOADING THE SAMPLE REPLACES IT (A6, the DQ platform's own load behaviour): clear the
    # leftovers of an earlier run — the `aml_case` nodes it opened and the `related_to` edges the
    # Network Mapper inferred — before writing the bank's data, so a fresh load is a clean state.
    _clear_run_leftovers(connection, project_id)

    registered = []
    for server in aml_mcp_servers():
        row = mcp_registry.ensure_mcp_server(
            connection, project_id, server["name"], server["url"],
            risk_tier=server["risk_tier"], data_boundary="internal",
            description=server["description"],
            publisher={"id": "boss", "name": "BOSS", "kind": "system"},
            provenance={**builtin_provenance(), "sample": "aml"}, respect_operator=True)
        registered.append({"name": server["name"], "registered": row is not None})

    ds = generate()
    ids: dict[tuple[str, str], str] = {}

    def node(kind: str, sample_id: str, name: str, body: str, attrs: dict[str, Any]) -> str:
        ids[(kind, sample_id)] = STORE.upsert_node(
            connection, project_id=project_id, kind=kind, node_key=node_key(kind, sample_id),
            name=name, body=body, trust_tier="observed", actor_kind="system",
            actor_ref=ACTOR_REF, source="aml_sample", attrs={**attrs, "sample": "aml"})
        return ids[(kind, sample_id)]

    def edge(src: str, dst: str, kind: str, attrs: dict[str, Any] | None = None) -> None:
        STORE.upsert_edge(connection, project_id=project_id, src_id=src, dst_id=dst, kind=kind,
                          actor_kind="system", actor_ref=ACTOR_REF, attrs=attrs or {})

    for typ in ds.typologies.values():
        node("typology", typ["typology_id"], typ["name"], typ["red_flags"], typ)
    for cid, cust in ds.customers.items():
        # Display name carries no raw id (A4): the id lives in node_key and attrs, never on the
        # name a decision-card subject or a screen shows a person.
        node("customer", cid, cust['name'],
             f"{cust['segment'].replace('_', ' ')} customer of {BANK} since "
             f"{cust['onboarded']}; {cust['occupation']}; risk {cust['risk_rating']}.", cust)
        kyc = ds.kyc_profiles[cid]
        kyc_id = node("kyc_profile", cid, f"KYC: {cust['name']}",
                      f"Expected activity: {kyc['expected_activity']} Source of funds: "
                      f"{kyc['source_of_funds']} Documents: {'; '.join(kyc['documents'])}.",
                      kyc)
        edge(ids[("customer", cid)], kyc_id, "has_kyc")
    for aid, acc in ds.accounts.items():
        owner = ds.customers[acc["customer_id"]]["name"]
        # Display name carries no raw id (A4/A8): the account number lives in attrs, never on the
        # name a graph screen shows a person.
        node("account", aid, f"{acc['product'].replace('_', ' ').title()} — {owner}",
             f"{acc['product'].replace('_', ' ')} of {owner}, opened {acc['opened']}.", acc)
        edge(ids[("customer", acc["customer_id"])], ids[("account", aid)], "owns")
    for cp_id, cp in ds.counterparties.items():
        where = f", registered at {cp['registered_address']}" if cp.get("registered_address") else ""
        node("counterparty", cp_id, cp['name'],
             f"{cp['kind']} counterparty in {cp['country']}{where}.", cp)

    def party_node(party: str) -> str:
        if party in ds.accounts:
            return ids[("account", party)]
        if party in ds.counterparties:
            return ids[("counterparty", party)]
        return ""

    # Aggregated flows: one transfers_to edge per (account, party) with what moved.
    flows: dict[tuple[str, str], dict[str, Any]] = {}
    for t in ds.transactions:
        if t["direction"] != "out":
            continue
        dst = party_node(t["party"])
        if not dst:
            continue
        row = flows.setdefault((t["account_id"], t["party"]),
                               {"count": 0, "total": 0.0, "first": t["booked_at"],
                                "last": t["booked_at"], "channels": set()})
        row["count"] += 1
        row["total"] = round(row["total"] + t["amount"], 2)
        row["last"] = t["booked_at"]
        row["channels"].add(t["channel"])
    for (account_id, party), row in flows.items():
        edge(ids[("account", account_id)], party_node(party), "transfers_to",
             {"count": row["count"], "total": row["total"], "first": row["first"],
              "last": row["last"], "channels": sorted(row["channels"])})
    # Inbound flows from counterparties (salaries, the ring's money coming back).
    for t in ds.transactions:
        if t["direction"] == "in" and t["party"] in ds.counterparties:
            key = (t["party"], t["account_id"])
            row = flows.setdefault(key, {"count": 0, "total": 0.0, "first": t["booked_at"],
                                         "last": t["booked_at"], "channels": set(), "in": True})
            row["count"] += 1
            row["total"] = round(row["total"] + t["amount"], 2)
            row["last"] = t["booked_at"]
            row["channels"].add(t["channel"])
    for (party, account_id), row in flows.items():
        if row.get("in"):
            edge(ids[("counterparty", party)], ids[("account", account_id)], "transfers_to",
                 {"count": row["count"], "total": row["total"], "first": row["first"],
                  "last": row["last"], "channels": sorted(row["channels"])})

    for a, b, why in ds.relationships:
        kind_a = "customer" if a in ds.customers else "counterparty"
        kind_b = "customer" if b in ds.customers else "counterparty"
        edge(ids[(kind_a, a)], ids[(kind_b, b)], "related_to", {"reason": why})

    by_txn = {t["transaction_id"]: t for t in ds.transactions}
    for alert in ds.alerts.values():
        owner_name = ds.customers.get(alert["customer_id"], {}).get("name") or "a customer"
        alert_id = node("alert", alert["alert_id"],
                        f"{alert['rule']} alert on {owner_name}",
                        f"{alert['rule_text']} (score {alert['score']}, raised "
                        f"{alert['raised_at']}).",
                        {k: v for k, v in alert.items() if k != "flagged_transactions"}
                        | {"typology_id": alert["typology_ids"][0],
                           "flagged_count": len(alert["flagged_transactions"])})
        edge(alert_id, ids[("account", alert["account_id"])], "raised_on")
        edge(alert_id, ids[("customer", alert["customer_id"])], "raised_on")
        for typology_id in alert["typology_ids"]:
            edge(alert_id, ids[("typology", typology_id)], "matches_typology")
        for txn_id in alert["flagged_transactions"]:
            t = by_txn[txn_id]
            txn = node("transaction", txn_id,
                       f"{t['direction'].title()} {t['amount']:,.2f} via {t['channel']}",
                       f"{t['booked_at']}: {t['direction']} {t['amount']:,.2f} via "
                       f"{t['channel']} {'to' if t['direction'] == 'out' else 'from'} "
                       f"{t['party']} ({t['memo']}).", t)
            edge(alert_id, txn, "flags")
            party = party_node(t["party"])
            if party:
                edge(txn, party, "transfers_to")

    # The named people who finish the two human gates with separation of duties (A5): an AML
    # Analyst who can escalate, and a BSA Officer who alone can file the SAR. Their owner_id is
    # the actor ref each approves with; they are `owner` nodes (owner_type person), the way the
    # DQ platform records the bank's people.
    people = [
        ("dana.analyst", "Dana Whitfield", "AML Analyst"),
        ("lee.analyst", "Lee Brennan", "AML Analyst"),
        ("bsa.officer", "Morgan Avery", "BSA Officer"),
    ]
    for owner_id, display, title in people:
        node("owner", owner_id, display,
             f"{display}, {title} at {BANK}.",
             {"owner_id": owner_id, "owner_type": "person", "title": title,
              "display_name": display})

    policy = _ingest_policy(connection, project_id, ids)

    installed = _install_workflow(connection, project_id, actor)
    counts = {kind: sum(1 for (k, _s) in ids if k == kind)
              for kind in ("customer", "account", "counterparty", "kyc_profile", "typology",
                           "alert", "transaction")}
    return {"ok": True, "pack_id": PACK_ID, "connectors": registered, "counts": counts,
            "flows": len(flows), "alerts": status(connection, project_id=project_id)["alerts"],
            "workflow": installed, "people": [p[0] for p in people], "policy": policy,
            "note": (f"Loaded {BANK}'s AML sample: {counts['customer']} customers, "
                     f"{counts['account']} accounts, {len(ds.transactions)} transactions "
                     f"(in the transactions connector), 3 open alerts. Start "
                     f"{WORKFLOW_KEY} with an alert to investigate it.")}


def _install_workflow(connection: Connection, project_id: str, actor: str) -> dict[str, Any]:
    from ..workflow_catalogue import seed

    result = seed(connection, project_id, actor=actor)
    for item in result.get("installed") or []:
        if item.get("key") == WORKFLOW_KEY:
            return item
    return {"key": WORKFLOW_KEY, "skipped": "not in the catalogue"}


def build_problem_screen(connection: Connection, *, project_id: str) -> dict[str, Any]:
    """The readable problem screen (A4): each alert's customer by name and the rule it tripped,
    never a raw customer id or node id, with an open count that reflects alerts a person has not
    yet closed or filed — not a count fixed at load time."""
    from tools.aml_sample_data import generate

    ds = generate()
    rows: list[dict[str, Any]] = []
    for alert_id, alert in ds.alerts.items():
        customer = ds.customers.get(alert["customer_id"]) or {}
        # A readable account label carries the product, never the raw account number (A4/A8):
        # the account id lives in attrs, never on a screen a person reads.
        account = ds.accounts.get(alert["account_id"]) or {}
        account_label = (account.get("product") or alert.get("product") or "account").replace(
            "_", " ").title()
        case = connection.execute(
            text("SELECT attrs FROM kg_nodes WHERE project_id = :p AND kind = 'aml_case' "
                 "AND node_key = :k AND valid_to IS NULL ORDER BY recorded_at DESC LIMIT 1"),
            {"p": project_id, "k": f"aml_case:{alert_id}"}).mappings().first()
        case_attrs = dict((case or {}).get("attrs") or {})
        case_status = str(case_attrs.get("status") or "open")
        is_open = case_status == "open"
        rows.append({
            "alert_id": alert_id,
            "customer_name": customer.get("name") or "Unnamed customer",
            "account_label": account_label,
            "rule": alert["rule"],
            "rule_text": alert["rule_text"],
            "score": alert["score"],
            "flagged_count": len(alert["flagged_transactions"]),
            "raised_at": alert["raised_at"],
            "status": case_status,
            "is_open": is_open,
        })
    open_count = len([r for r in rows if r["is_open"]])
    return {"alerts": rows, "open_count": open_count}


def _clear_run_leftovers(connection: Connection, project_id: str) -> None:
    """Remove every leftover of an earlier run of the AML workflow — the `aml_case` nodes it
    opened, and the `related_to` edges the Network Mapper inferred (tagged with a run_id) — so a
    fresh load never mistakes BOSS's own prior output for the bank's network (A3/A6)."""
    removed_cases = connection.execute(text(
        "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'aml_case'"),
        {"p": project_id}).scalars().all()
    for node_id in removed_cases:
        try:
            with connection.begin_nested():
                STORE.delete_node(connection, project_id=project_id, node_id=node_id,
                                  actor_kind="person")
        except Exception:  # noqa: BLE001 — already gone, or edges removed it already
            pass
    connection.execute(text(
        "DELETE FROM kg_edges WHERE project_id = :p AND kind = 'related_to' "
        "AND attrs->>'run_id' IS NOT NULL"), {"p": project_id})


def _ingest_policy(connection: Connection, project_id: str,
                   ids: dict[tuple[str, str], str]) -> dict[str, Any]:
    """Chapter 2 (A1): the AML monitoring policy becomes knowledge through the document path the
    DQ rulebook uses — a document with cited clauses, ingested by a named person — and each alert
    rule's typology and alerts are linked to the policy page that defines them, so the Analyst can
    cite 'policy §FANOUT-01 p.4'."""
    from . import documents

    clauses = [
        {"section": "FANOUT-01",
         "text": "Ten or more outbound payments to distinct payees from one account in a single "
                 "day, above the fan-out threshold, are flagged as a rapid fan-out (funnel "
                 "account) typology.",
         "citation": "NorthPeak AML Standard v1 p.4", "page": 4},
        {"section": "STRUCT-02",
         "text": "Three or more cash deposits between $9,000 and the $10,000 CTR threshold (31 CFR "
                 "1010.311), followed by outbound wires, are flagged as structuring.",
         "citation": "NorthPeak AML Standard v1 p.7", "page": 7},
    ]
    result = documents.add_document(
        connection, project_id=project_id,
        title="NorthPeak AML Transaction Monitoring Standard",
        source_url="internal://aml-policy", citation="NorthPeak AML Standard v1",
        clauses=clauses, actor_kind="person", actor_ref="dana.analyst")
    # Link the rule's typology (and the alerts that tripped it) to the clause that defines it.
    links = {"FANOUT-01": ("TYP-FANOUT", ["ALERT-1", "ALERT-2"]),
             "STRUCT-02": ("TYP-STRUCTURING", ["ALERT-3"])}
    for clause_id, clause in zip(result.get("clause_ids") or [], clauses):
        typ_key, alert_keys = links.get(clause["section"], ("", []))
        for src in [ids.get(("typology", typ_key))] + [ids.get(("alert", a)) for a in alert_keys]:
            if src:
                STORE.upsert_edge(connection, project_id=project_id, src_id=src, dst_id=clause_id,
                                  kind="implemented_by", actor_kind="system", actor_ref=ACTOR_REF,
                                  attrs={"page": clause["page"], "citation": clause["citation"]})
    return {"document_id": result.get("document_id"), "clauses": len(result.get("clause_ids") or [])}


def reload_sample(connection: Connection, *, project_id: str, actor: str) -> dict[str, Any]:
    """Load the sample again (A6, the DQ platform's own reload behaviour): the bank's data stays
    exactly as loaded, idempotently, while `load_sample` clears every leftover of an earlier run
    first, so a fresh run never mistakes BOSS's own prior output for the bank's network."""
    return load_sample(connection, project_id=project_id, actor=actor)


def status(connection: Connection, *, project_id: str) -> dict[str, Any]:
    """Whether the sample is loaded, and the alerts with each one's latest investigation."""
    from tools.aml_sample_data import generate
    from . import ontology_packs

    pack = ontology_packs.get_project_pack(connection, project_id=project_id)
    alerts = []
    for alert_id, alert in generate().alerts.items():
        node = node_id_of(connection, project_id, "alert", alert_id)
        case = connection.execute(
            text("SELECT id, attrs FROM kg_nodes WHERE project_id = :p AND kind = 'aml_case' "
                 "AND node_key = :k AND (valid_to IS NULL OR valid_to > now()) "
                 "ORDER BY recorded_at DESC LIMIT 1"),
            {"p": project_id, "k": f"aml_case:{alert_id}"}).mappings().first() if node else None
        attrs = (case or {}).get("attrs") or {}
        alerts.append({"alert_id": alert_id, "node_id": node or None,
                       "account_id": alert["account_id"], "customer_id": alert["customer_id"],
                       "rule": alert["rule"], "raised_at": alert["raised_at"],
                       "case_id": (case or {}).get("id"),
                       "case_status": attrs.get("status"),
                       "determination": attrs.get("determination")})
    loaded = any(a["node_id"] for a in alerts)
    return {"loaded": loaded, "pack_id": pack.pack_id if pack else None,
            "workflow_key": WORKFLOW_KEY, "alerts": alerts}
