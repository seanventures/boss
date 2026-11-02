"""NorthPeak Bank's loan data-quality world: the single source of truth for the banking DQ case study.

DETERMINISTIC AND SYNTHETIC. Every value is a fixed table below or comes from `random.Random(seed)`
(sample keys and profile baselines only). No network, no model, no clock. `generate()` returns the
same world on every machine, so the rulebook PDF (tools/dq_sample/rulebook.py), the mock MCP
servers (DQ Monitor, Service Desk, Profile Service), the graph loader and the tests all read
one bank.

What is here (see README.md for the full API and the relationship names):
  * the Knowledge Graph (reference world, read only for agents): systems, owners, database
    structures/tables/columns (real Apache Fineract names where the bank's core is Fineract),
    feeds, column-level lineage, 24 CDEs, 96 DQ rules with Warning/Error thresholds, regulatory
    reports, report lines and MDRM codes, the business calendar, issues and examination findings;
  * the Context Graph seed (what agents did before): 10 closed incidents with confirmed causes,
    fixes and fix durability, their precedents and resolved patterns, and one open incident;
  * the DQ Monitor's exception groups over COB 2026-10-01 .. 2026-10-07 (incl. the running example
    DQX-0588 v2 on COB 2026-10-05) and the Profile Service's exception profiles per data platform;
  * `answer_key()`: the expected pipeline outcome per exception group, and every rulebook rule's
    fields (for measuring rulebook extraction). Never shown to an agent.

MDRM codes and report line numbers are ILLUSTRATIVE: they follow the public FFIEC 031/041 Call
Report and FR Y-9C schedules but must be verified against the FFIEC MDRM dictionary before any
real use. The rulebook front matter says so.

Pure standard library.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

SEED = 20261005
BANK = "NorthPeak Bank"
RULEBOOK_TITLE = "NorthPeak Bank: Loan Data Quality Rulebook"
RULEBOOK_VERSION = "3.2"
RULEBOOK_EFFECTIVE = "2026-07-01"
RUNNING_EXAMPLE = "EXG-20261005-0003"   # DQX-0588 v2 on COB 2026-10-05
MDRM_DISCLAIMER = ("Report line numbers and MDRM codes in this rulebook are illustrative. They "
                   "follow the public FFIEC 031/041 Call Report and FR Y-9C schedules; verify "
                   "every code against the FFIEC MDRM dictionary before relying on it.")

# ---------------------------------------------------------------------------------------------
# Kind names (snake_case). The ontology pack mirrors exactly these.
# ---------------------------------------------------------------------------------------------
KG_KINDS = ("system", "owner", "feed", "lineage", "database_structure", "database_table",
            "database_column", "cde", "data_quality_rule", "dq_threshold", "regulatory_report",
            "report_line", "mdrm", "business_calendar", "calendar_day", "issue",
            "examination_finding", "transformation", "root_cause_category",
            "remediation_template")
#: Kinds the pipeline's agent designs read that this sample world holds no instance of: the
#: pack declares them (so a bank's own graph can carry them and the contracts read them), but
#: the sample never invents one.
DECLARED_ONLY_KINDS = ("derivation_rule", "data_concern", "adjustment",
                       "regulatory_communication", "join_path", "domain", "contract")
CONTEXT_KINDS = ("exception_group", "incident", "precedent", "resolved_pattern")

#: Every relationship the data emits: (source kind, relationship, target kind, meaning).
RELATIONSHIPS: tuple[tuple[str, str, str, str], ...] = (
    ("owner", "OWNS", "system", "the team/person accountable for a system"),
    ("owner", "OWNS", "feed", "the team that runs a feed"),
    ("owner", "OWNS", "data_quality_rule", "the rule owner (Service Desk tickets go to its group)"),
    ("owner", "STEWARDS", "cde", "the data steward of a critical data element"),
    ("owner", "MEMBER_OF", "owner", "a person belongs to a team"),
    ("system", "HOSTS", "database_structure", "a system hosts a database/schema"),
    ("system", "OWNS", "database_table", "the system of record for a table"),
    ("system", "USES_CALENDAR", "business_calendar", "the calendar a system's COB follows"),
    ("database_structure", "CONTAINS", "database_table", "a schema contains a table"),
    ("database_table", "HAS_COLUMN", "database_column", "a table has a column"),
    ("feed", "FROM_SYSTEM", "system", "a feed's source system"),
    ("feed", "TO_SYSTEM", "system", "a feed's target system"),
    ("feed", "READS", "database_table", "a feed extracts from a table"),
    ("feed", "FEEDS", "database_table", "a feed loads a table"),
    ("lineage", "FROM", "database_column", "a lineage hop starts at a column"),
    ("lineage", "TO", "database_column", "a lineage hop lands in a column"),
    ("lineage", "TO", "report_line", "the last hop lands on a report line"),
    ("lineage", "VIA", "feed", "the feed that carries a hop"),
    ("lineage", "APPLIES", "transformation", "the transformation a hop applies on the way"),
    ("cde", "STORED_IN", "database_column", "where a CDE's value of record lives"),
    ("cde", "REPORTED_IN", "report_line", "a report line the CDE feeds"),
    ("cde", "DERIVED_FROM", "cde", "upstream CDE (business lineage)"),
    ("cde", "SYSTEM_OF_RECORD", "system", "the CDE's system of record"),
    ("data_quality_rule", "CHECKS", "cde", "the CDE a rule protects"),
    ("data_quality_rule", "APPLIES_TO", "database_column", "a column the check reads"),
    ("data_quality_rule", "APPLIES_TO", "database_table", "the table the check runs on"),
    ("data_quality_rule", "COVERS", "feed", "the feed whose output the rule checks"),
    ("data_quality_rule", "MONITORED_BY", "system", "the DQ monitor that runs it (the DQ Monitor)"),
    ("data_quality_rule", "REACHES", "report_line", "regulatory reach of the rule"),
    ("data_quality_rule", "RELATED_TO", "data_quality_rule", "a related rule"),
    ("dq_threshold", "OF", "data_quality_rule", "a rule's Warning or Error threshold"),
    ("report_line", "PART_OF", "regulatory_report", "a line belongs to a report"),
    ("report_line", "HAS_MDRM", "mdrm", "the MDRM code of a report line"),
    ("mdrm", "DEFINED_FOR", "regulatory_report", "the report an MDRM code belongs to"),
    ("calendar_day", "IN", "business_calendar", "a day in a calendar"),
    ("remediation_template", "FOR_CATEGORY", "root_cause_category",
     "the root-cause family a fix template addresses"),
    ("issue", "CONCERNS", "system", "a known issue affects a system"),
    ("issue", "CONCERNS", "feed", "a known issue affects a feed"),
    ("issue", "CONCERNS", "data_quality_rule", "a known issue affects a rule"),
    ("issue", "CONCERNS", "database_table", "a known issue affects a table"),
    ("issue", "TRACKS", "examination_finding", "the issue that remediates a finding"),
    ("issue", "OWNED_BY", "owner", "who works the issue"),
    ("examination_finding", "CONCERNS", "report_line", "a finding cites a report line"),
    ("examination_finding", "CONCERNS", "data_quality_rule", "a finding cites a rule"),
    ("examination_finding", "CONCERNS", "cde", "a finding cites a CDE"),
    ("examination_finding", "CONCERNS", "feed", "a finding cites a feed"),
    ("examination_finding", "CONCERNS", "business_calendar", "a finding cites the calendar"),
    # --- Context Graph ---
    ("exception_group", "RAISED_BY", "data_quality_rule", "the rule whose check failed"),
    ("exception_group", "ON_COB", "calendar_day", "the close of business graded"),
    ("exception_group", "AFFECTS", "database_column", "the failing column"),
    ("exception_group", "EMITTED_BY", "system", "the DQ monitor that emitted it (the DQ Monitor)"),
    ("incident", "RAISED_FROM", "exception_group", "the exception group that opened it"),
    ("incident", "CONCERNS", "data_quality_rule", "the rule an incident is about"),
    ("incident", "CONCERNS", "cde", "the CDE an incident is about"),
    ("incident", "CAUSED_BY", "resolved_pattern", "the confirmed root-cause pattern"),
    ("incident", "ASSIGNED_TO", "owner", "the owner working the incident"),
    ("incident", "CORRELATED_WITH", "incident", "a recurrence of an earlier incident"),
    ("precedent", "FROM_INCIDENT", "incident", "the closed incident a precedent records"),
    ("precedent", "MATCHES", "resolved_pattern", "the pattern a precedent confirms"),
    ("resolved_pattern", "SEEN_ON", "data_quality_rule", "a rule the pattern has broken"),
    ("resolved_pattern", "IN_CATEGORY", "root_cause_category", "the pattern's root-cause family"),
    ("resolved_pattern", "FIXED_BY", "remediation_template", "the durable fix that held"),
)

DIMENSIONS = ("completeness", "validity", "accuracy", "consistency", "timeliness", "uniqueness")
ROOT_CAUSE_FAMILIES = {
    "source_processing": "A defect or job-ordering problem in the source system's own processing.",
    "feed_etl": "A feed landed late, partial or transformed wrongly between systems.",
    "reference_data": "Configuration or mapping data (products, GL accounts, codes) is wrong.",
    "manual_entry": "Data captured wrongly or twice by people or front-end channels.",
    "rule_config": "The DQ rule or its threshold is wrong: the data is fine (false positive).",
}
ROOT_CAUSE_FAMILY_NAMES = {
    "source_processing": "Source processing", "feed_etl": "Feed or ETL",
    "reference_data": "Reference data", "manual_entry": "Manual entry",
    "rule_config": "Rule configuration (false positive)",
}
#: A lineage hop that only copies or files a value transforms nothing: no Transformation node.
PLAIN_TRANSFORMS = frozenset({"copied", "copied as at COB", "filed on the line"})


def remediation_template_id(pattern_id: str) -> str:
    """RPAT-COB-ORDER's durable fix is the remediation template RTPL-COB-ORDER."""
    return "RTPL-" + pattern_id.removeprefix("RPAT-")


#: Pipeline lanes: one per root-cause family, plus the two side paths.
LANES = {
    "lane_source_processing": "source_processing", "lane_feed_etl": "feed_etl",
    "lane_reference_data": "reference_data", "lane_manual_entry": "manual_entry",
    "rule_drafting": "rule_config",          # false positive -> Rule Proposal Agent case
    "insufficient_evidence": None,           # RCA loops back to Context Hydration + Past Incidents
}


# ---------------------------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Owner:
    owner_id: str
    name: str
    owner_type: str                 # "team" | "person"
    title: str
    team_id: str | None = None      # a person's team
    service_desk_group: str | None = None   # Service Desk assignment group (teams)


@dataclass(frozen=True)
class System:
    system_id: str
    name: str
    description: str
    tier: int                       # 1 = regulatory-critical
    owner_id: str
    platform: str                   # technology
    role: str                       # "source" | "ledger" | "warehouse" | "reporting" | "dq_monitor" ...
    mcp_server: str | None = None   # external systems reached via MCP


@dataclass(frozen=True)
class DatabaseStructure:
    structure_id: str
    name: str
    system_id: str
    engine: str
    description: str


@dataclass(frozen=True)
class DatabaseColumn:
    key: str                        # "<table>.<column>"
    table: str
    name: str
    data_type: str
    description: str


@dataclass(frozen=True)
class DatabaseTable:
    key: str                        # Fineract tables keep their plain name (m_loan); others schema.table
    name: str
    structure_id: str
    system_id: str
    description: str
    fineract: bool                  # a real Apache Fineract table
    in_code_graph: bool             # indexed as a `table` node in project northpeak-lending
    columns: tuple[DatabaseColumn, ...]


@dataclass(frozen=True)
class Feed:
    feed_id: str
    name: str
    source_system: str
    target_system: str
    reads: tuple[str, ...]
    feeds: tuple[str, ...]
    frequency: str
    sla: str
    owner_id: str


@dataclass(frozen=True)
class Lineage:
    lineage_id: str
    from_column: str
    to: str                         # a column key, or a report_line id when to_kind == "report_line"
    to_kind: str                    # "database_column" | "report_line"
    feed_id: str | None
    transform: str


@dataclass(frozen=True)
class Cde:
    cde_id: str
    name: str
    definition: str
    system_of_record: str
    columns: tuple[str, ...]
    steward_id: str
    upstream: tuple[str, ...]
    reported_in: tuple[str, ...]


@dataclass(frozen=True)
class RegulatoryReport:
    report_id: str
    name: str
    form: str
    frequency: str
    regulator: str


@dataclass(frozen=True)
class ReportLine:
    line_id: str
    report_id: str
    schedule: str
    item: str
    caption: str
    mdrm: str


@dataclass(frozen=True)
class Mdrm:
    code: str
    mnemonic: str
    item_code: str
    description: str
    report_id: str


@dataclass(frozen=True)
class DqThreshold:
    threshold_id: str               # "<rule id>:warning" | "<rule id>:error"
    rule_id: str
    level: str                      # "Warning" | "Error"
    metric: str                     # rate | count | minutes | relative_diff | amount_usd
    value: float
    also: str                       # an extra clause ("or any single loan differs by ...")
    text: str                       # the sentence printed in the rulebook


@dataclass(frozen=True)
class DqRule:
    rule_id: str
    version: int
    monitor_id: str
    family: str
    title: str
    statement: str
    dimension: str
    cde_ids: tuple[str, ...]
    columns: tuple[str, ...]
    tables: tuple[str, ...]
    check: str
    population: str
    warning: DqThreshold
    error: DqThreshold
    severity: str                   # High | Medium | Low
    owner_id: str
    system_id: str
    feed_id: str | None
    report_lines: tuple[str, ...]
    handling: str
    related: tuple[str, ...]
    legal_reference: str | None = None
    status: str = "active"          # "active" | "retired"


@dataclass(frozen=True)
class CalendarDay:
    day: str
    weekday: str
    business_day: bool
    holiday: str | None
    quarter_end: bool


@dataclass(frozen=True)
class Incident:
    incident_id: str
    title: str
    opened: str
    closed: str | None
    status: str                     # closed | investigating
    rule_id: str
    cde_ids: tuple[str, ...]
    severity: str
    owner_id: str
    root_cause_family: str | None
    pattern_id: str | None
    confirmed_cause: str | None
    ruled_out: tuple[str, ...]
    fix: str | None
    fix_type: str | None            # data_patch | code_fix | config_change | process_change | ...
    fix_durability: str | None      # held | did_not_hold | held_until_regressed | partial
    durability_note: str | None
    correlated_with: tuple[str, ...] = ()
    raised_from: str | None = None  # exception group id (open incidents)


@dataclass(frozen=True)
class ResolvedPattern:
    pattern_id: str
    name: str
    family: str
    description: str
    signature: str
    rule_ids: tuple[str, ...]
    durable_fix: str


@dataclass(frozen=True)
class Precedent:
    precedent_id: str
    incident_id: str
    pattern_id: str
    summary: str


@dataclass(frozen=True)
class Issue:
    issue_id: str
    title: str
    status: str
    opened: str
    owner_id: str
    concerns: tuple[tuple[str, str], ...]    # (kind, key)
    tracks: tuple[str, ...] = ()             # examination finding ids
    description: str = ""


@dataclass(frozen=True)
class ExaminationFinding:
    finding_id: str
    title: str
    severity: str                   # MRA | Finding | Observation
    regulator: str
    issued: str
    due: str
    status: str
    concerns: tuple[tuple[str, str], ...]
    description: str


@dataclass(frozen=True)
class ExceptionGroup:
    group_id: str
    monitor_rule_id: str
    rule_id: str
    rule_version: int
    cob: str
    platform: str                   # system id of the data platform checked
    table: str
    column: str
    population: int
    failed: int
    metric: str
    measured: float                 # the value graded (rate, count, minutes, diff ...)
    monitor_grade: str                # the grade the DQ Monitor emitted
    monitor_thresholds: dict[str, float]
    emitted_at: str
    sample_keys: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True)
class Change:
    change_id: str
    day: str
    system_id: str
    description: str
    rule_ids: tuple[str, ...]
    feed_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlatformProfile:
    platform: str                   # system id
    daily_rows: int
    rules: dict[str, dict[str, Any]]  # rule id -> baseline_rate_30d, stdev, last_7
    recent_changes: tuple[Change, ...]


# ---------------------------------------------------------------------------------------------
# Owners and systems
# ---------------------------------------------------------------------------------------------
OWNERS: tuple[Owner, ...] = (
    Owner("OWN-CDO", "Chief Data Office", "team", "Data governance", service_desk_group="NPB-CDO"),
    Owner("OWN-LOANOPS", "Loan Operations", "team", "Loan servicing operations",
          service_desk_group="NPB-LOAN-OPS"),
    Owner("OWN-PAYOPS", "Payments Operations", "team", "Payments and posting",
          service_desk_group="NPB-PAY-OPS"),
    Owner("OWN-COLL", "Collections", "team", "Arrears and collections", service_desk_group="NPB-COLL"),
    Owner("OWN-CUSTDATA", "Customer Data Management", "team", "Customer master data",
          service_desk_group="NPB-CUST-DATA"),
    Owner("OWN-FIN", "Finance Controllership", "team", "General ledger and provisioning",
          service_desk_group="NPB-FIN-CTRL"),
    Owner("OWN-REGREP", "Regulatory Reporting", "team", "Call Report and FR Y-9C",
          service_desk_group="NPB-REG-REP"),
    Owner("OWN-DATAPLAT", "Data Platform Engineering", "team", "Warehouse and feeds",
          service_desk_group="NPB-DATA-PLAT"),
    Owner("OWN-LSPENG", "Loan Platform Engineering", "team", "Fineract platform engineering",
          service_desk_group="NPB-LSP-ENG"),
    Owner("OWN-DQOPS", "Data Quality Operations", "team", "DQ Monitor administration",
          service_desk_group="NPB-DQ-OPS"),
    Owner("P-DWHITFIELD", "Dana Whitfield", "person", "Chief Data Officer", "OWN-CDO"),
    Owner("P-MFELD", "Marcus Feld", "person", "Loan Operations data steward", "OWN-LOANOPS"),
    Owner("P-ARAHMAN", "Aisha Rahman", "person", "Payments data steward", "OWN-PAYOPS"),
    Owner("P-TLINDGREN", "Tomas Lindgren", "person", "Collections data steward", "OWN-COLL"),
    Owner("P-PNATARAJAN", "Priya Natarajan", "person", "Customer data steward", "OWN-CUSTDATA"),
    Owner("P-HOKAFOR", "Helen Okafor", "person", "Finance data steward", "OWN-FIN"),
    Owner("P-VOSEI", "Victor Osei", "person", "Regulatory reporting data steward", "OWN-REGREP"),
    Owner("P-JKIM", "Jonah Kim", "person", "Data platform lead", "OWN-DATAPLAT"),
    Owner("P-EVARGA", "Elena Varga", "person", "Loan platform lead", "OWN-LSPENG"),
    Owner("P-RMENON", "Ravi Menon", "person", "DQ operations lead", "OWN-DQOPS"),
)

SYSTEMS: tuple[System, ...] = (
    System("S-LSP", "Loan Servicing Platform", "Apache Fineract loan core: loans, schedules, "
           "transactions, charges, arrears aging and loan accounting.", 1, "OWN-LSPENG",
           "Apache Fineract on PostgreSQL", "source"),
    System("S-GL", "General Ledger", "The bank's book of record; receives loan journals from the "
           "LSP intraday.", 1, "OWN-FIN", "Ledger on PostgreSQL", "ledger"),
    System("S-PAY", "Payments Hub", "Inbound repayments (ACH, card, branch) routed to the LSP.", 2,
           "OWN-PAYOPS", "Payments hub", "source"),
    System("S-CIF", "Customer Master", "Customer information file; golden record for clients.", 1,
           "OWN-CUSTDATA", "Customer master", "source"),
    System("S-COL", "Collections", "Arrears workflow and collection cases.", 2, "OWN-COLL",
           "Collections platform", "source"),
    System("S-CLT", "Collateral Management", "Collateral items and valuations for secured "
           "loans.", 3, "OWN-LOANOPS", "Collateral platform", "source"),
    System("S-EDW", "Enterprise Data Warehouse", "Daily loan, transaction and arrears snapshots "
           "for reporting and finance.", 1, "OWN-DATAPLAT", "Warehouse", "warehouse"),
    System("S-RRH", "Regulatory Reporting Hub", "Builds the Call Report (FFIEC 031) and FR Y-9C "
           "extracts.", 1, "OWN-REGREP", "Reporting hub", "reporting"),
    System("S-DQM", "DQ Monitor", "Runs every DQ rule at each COB, grades it Warning or Error "
           "and emits exception groups.", 1, "OWN-DQOPS", "DQ Monitor", "dq_monitor", "dq-monitor"),
    System("S-DESK", "Service Desk", "Incident and change tickets; assignment groups per "
           "owner.", 2, "OWN-DQOPS", "Service Desk", "service_desk", "service-desk"),
    System("S-PRF", "Profile Service", "Deterministic exception profiles per data platform "
           "(baseline failure rates, recent changes).", 2, "OWN-DATAPLAT", "Profile Service",
           "profile_service", "profile-service"),
)

STRUCTURES: tuple[DatabaseStructure, ...] = (
    DatabaseStructure("DS-LSP-FINERACT", "fineract_default", "S-LSP", "PostgreSQL",
                      "Fineract tenant database (default tenant)."),
    DatabaseStructure("DS-GL", "gl", "S-GL", "PostgreSQL", "General ledger schema."),
    DatabaseStructure("DS-PAY", "pay", "S-PAY", "PostgreSQL", "Payments hub schema."),
    DatabaseStructure("DS-CIF", "cif", "S-CIF", "PostgreSQL", "Customer master schema."),
    DatabaseStructure("DS-COL", "col", "S-COL", "PostgreSQL", "Collections schema."),
    DatabaseStructure("DS-CLT", "clt", "S-CLT", "PostgreSQL", "Collateral schema."),
    DatabaseStructure("DS-EDW", "edw", "S-EDW", "Columnar warehouse", "Loan data marts."),
    DatabaseStructure("DS-RRH", "rrh", "S-RRH", "PostgreSQL", "Regulatory extracts."),
)

#: Tables indexed as `table` nodes in project northpeak-lending (the Fineract code graph).
CODE_GRAPH_TABLES = frozenset({
    "m_loan", "m_loan_transaction", "m_loan_repayment_schedule", "m_loan_arrears_aging",
    "m_loan_charge", "m_charge", "m_client", "m_product_loan", "organisationcurrency",
    "acc_gl_journal_entry", "acc_product_mapping", "m_provisioning_history",
})

_C = tuple[str, str, str]   # (column, type, description)
_TABLE_SPECS: tuple[tuple[str, str, str, str, bool, tuple[_C, ...]], ...] = (
    # key, structure, system, description, fineract, columns
    ("m_loan", "DS-LSP-FINERACT", "S-LSP", "Loan accounts (one row per loan).", True, (
        ("id", "bigint", "Loan id"),
        ("account_no", "varchar(20)", "Loan account number"),
        ("external_id", "varchar(100)", "External reference"),
        ("client_id", "bigint", "Borrowing client (m_client.id)"),
        ("product_id", "bigint", "Loan product (m_product_loan.id)"),
        ("loan_status_id", "smallint", "Status: 100 submitted, 200 approved, 300 active, "
                                       "600 closed (obligations met), 601 written off, 700 overpaid"),
        ("currency_code", "varchar(3)", "ISO 4217 currency"),
        ("principal_amount", "decimal(19,6)", "Original (approved) principal"),
        ("principal_disbursed_derived", "decimal(19,6)", "Principal disbursed"),
        ("principal_repaid_derived", "decimal(19,6)", "Principal repaid"),
        ("principal_writtenoff_derived", "decimal(19,6)", "Principal written off"),
        ("principal_outstanding_derived", "decimal(19,6)", "Principal outstanding"),
        ("interest_outstanding_derived", "decimal(19,6)", "Interest outstanding"),
        ("fee_charges_outstanding_derived", "decimal(19,6)", "Fees outstanding"),
        ("penalty_charges_outstanding_derived", "decimal(19,6)", "Penalties outstanding"),
        ("total_outstanding_derived", "decimal(19,6)", "Total outstanding"),
        ("annual_nominal_interest_rate", "decimal(19,6)", "Annual nominal interest rate"),
        ("disbursedon_date", "date", "Disbursement date"),
        ("maturedon_date", "date", "Maturity date"),
        ("closedon_date", "date", "Closed date"),
        ("writtenoffon_date", "date", "Write-off date"),
        ("last_modified_on_utc", "timestamp", "Last modification time"),
    )),
    ("m_product_loan", "DS-LSP-FINERACT", "S-LSP", "Loan products.", True, (
        ("id", "bigint", "Product id"),
        ("name", "varchar(100)", "Product name"),
        ("min_principal_amount", "decimal(19,6)", "Minimum principal"),
        ("max_principal_amount", "decimal(19,6)", "Maximum principal"),
        ("min_nominal_interest_rate_per_period", "decimal(19,6)", "Minimum nominal rate"),
        ("max_nominal_interest_rate_per_period", "decimal(19,6)", "Maximum nominal rate"),
    )),
    ("organisationcurrency", "DS-LSP-FINERACT", "S-LSP", "Currencies the bank has enabled.", True, (
        ("code", "varchar(3)", "ISO 4217 code"),
    )),
    ("m_loan_transaction", "DS-LSP-FINERACT", "S-LSP", "Loan transactions.", True, (
        ("id", "bigint", "Transaction id"),
        ("loan_id", "bigint", "Loan (m_loan.id)"),
        ("transaction_type_enum", "smallint", "Type: 1 disbursement, 2 repayment, 6 write-off, "
                                              "8 recovery, 25 chargeback ..."),
        ("transaction_date", "date", "Business date of the transaction"),
        ("submitted_on_date", "date", "Date the transaction was posted"),
        ("amount", "decimal(19,6)", "Transaction amount"),
        ("principal_portion_derived", "decimal(19,6)", "Portion allocated to principal"),
        ("interest_portion_derived", "decimal(19,6)", "Portion allocated to interest"),
        ("fee_charges_portion_derived", "decimal(19,6)", "Portion allocated to fees"),
        ("penalty_charges_portion_derived", "decimal(19,6)", "Portion allocated to penalties"),
        ("overpayment_portion_derived", "decimal(19,6)", "Overpayment portion"),
        ("outstanding_loan_balance_derived", "decimal(19,6)", "Loan balance after the transaction"),
        ("is_reversed", "boolean", "Reversed flag"),
        ("reversed_on_date", "date", "Reversal date"),
        ("external_id", "varchar(100)", "External reference"),
    )),
    ("m_loan_repayment_schedule", "DS-LSP-FINERACT", "S-LSP", "Repayment schedule installments.",
     True, (
        ("id", "bigint", "Installment id"),
        ("loan_id", "bigint", "Loan (m_loan.id)"),
        ("installment", "smallint", "Installment number"),
        ("fromdate", "date", "Period start"),
        ("duedate", "date", "Due date"),
        ("principal_amount", "decimal(19,6)", "Scheduled principal"),
        ("principal_completed_derived", "decimal(19,6)", "Principal paid"),
        ("interest_amount", "decimal(19,6)", "Scheduled interest"),
        ("interest_completed_derived", "decimal(19,6)", "Interest paid"),
        ("fee_charges_amount", "decimal(19,6)", "Scheduled fees"),
        ("penalty_charges_amount", "decimal(19,6)", "Scheduled penalties"),
        ("completed_derived", "boolean", "Installment fully paid"),
        ("obligations_met_on_date", "date", "Date the installment was fully paid"),
    )),
    ("m_loan_arrears_aging", "DS-LSP-FINERACT", "S-LSP", "Arrears aging per loan.", True, (
        ("loan_id", "bigint", "Loan (m_loan.id)"),
        ("principal_overdue_derived", "decimal(19,6)", "Principal overdue"),
        ("interest_overdue_derived", "decimal(19,6)", "Interest overdue"),
        ("fee_charges_overdue_derived", "decimal(19,6)", "Fees overdue"),
        ("penalty_charges_overdue_derived", "decimal(19,6)", "Penalties overdue"),
        ("total_overdue_derived", "decimal(19,6)", "Total overdue"),
        ("overdue_since_date_derived", "date", "Overdue since"),
    )),
    ("m_loan_charge", "DS-LSP-FINERACT", "S-LSP", "Charges applied to loans.", True, (
        ("id", "bigint", "Loan charge id"),
        ("loan_id", "bigint", "Loan (m_loan.id)"),
        ("charge_id", "bigint", "Charge definition (m_charge.id)"),
        ("amount", "decimal(19,6)", "Charge amount"),
        ("amount_paid_derived", "decimal(19,6)", "Paid"),
        ("amount_waived_derived", "decimal(19,6)", "Waived"),
        ("amount_writtenoff_derived", "decimal(19,6)", "Written off"),
        ("amount_outstanding_derived", "decimal(19,6)", "Outstanding"),
        ("is_paid_derived", "boolean", "Paid flag"),
        ("is_active", "boolean", "Active flag"),
        ("due_for_collection_as_of_date", "date", "Due date for specified-due-date charges"),
    )),
    ("m_charge", "DS-LSP-FINERACT", "S-LSP", "Charge definitions.", True, (
        ("id", "bigint", "Charge id"),
        ("name", "varchar(100)", "Charge name"),
    )),
    ("m_client", "DS-LSP-FINERACT", "S-LSP", "Clients (borrowers).", True, (
        ("id", "bigint", "Client id"),
        ("account_no", "varchar(20)", "Client account number"),
        ("external_id", "varchar(100)", "Customer Master (CIF) id"),
        ("status_enum", "int", "Status: 100 pending, 300 active, 600 closed"),
        ("display_name", "varchar(100)", "Display name"),
        ("date_of_birth", "date", "Date of birth"),
        ("activation_date", "date", "Activation date"),
        ("legal_form_enum", "int", "1 person, 2 entity"),
        ("office_id", "bigint", "Branch office"),
    )),
    ("acc_gl_journal_entry", "DS-LSP-FINERACT", "S-LSP", "Loan accounting journal entries.", True, (
        ("id", "bigint", "Journal entry id"),
        ("account_id", "bigint", "GL account (acc_gl_account.id)"),
        ("office_id", "bigint", "Office"),
        ("loan_transaction_id", "bigint", "Loan transaction (m_loan_transaction.id)"),
        ("entry_date", "date", "Entry date"),
        ("type_enum", "smallint", "1 credit, 2 debit"),
        ("amount", "decimal(19,6)", "Amount"),
        ("reversed", "boolean", "Reversed flag"),
        ("currency_code", "varchar(3)", "Currency"),
    )),
    ("acc_gl_account", "DS-LSP-FINERACT", "S-LSP", "GL chart of accounts in Fineract.", True, (
        ("id", "bigint", "GL account id"),
        ("gl_code", "varchar(45)", "GL code"),
        ("disabled", "boolean", "Disabled flag"),
    )),
    ("acc_product_mapping", "DS-LSP-FINERACT", "S-LSP", "Product to GL account mappings.", True, (
        ("gl_account_id", "bigint", "GL account"),
        ("product_id", "bigint", "Product"),
        ("financial_account_type", "smallint", "Mapping type (loan portfolio, interest income ...)"),
    )),
    ("m_provisioning_history", "DS-LSP-FINERACT", "S-LSP", "Provisioning runs.", True, (
        ("id", "bigint", "Provisioning run id"),
        ("created_date", "date", "Run date"),
    )),
    ("m_loanproduct_provisioning_entry", "DS-LSP-FINERACT", "S-LSP",
     "Provision amounts per product and category for each provisioning run.", True, (
        ("history_id", "bigint", "Provisioning run (m_provisioning_history.id)"),
        ("product_id", "bigint", "Product"),
        ("category_id", "bigint", "Provisioning category"),
        ("reseve_amount", "decimal(19,6)", "Provision amount (Fineract spells the column so)"),
    )),
    ("gl.ledger_balance", "DS-GL", "S-GL", "Daily GL balances.", False, (
        ("gl_account", "varchar(20)", "GL account"),
        ("cob_date", "date", "COB date"),
        ("balance", "decimal(19,2)", "Closing balance"),
    )),
    ("pay.payment_instruction", "DS-PAY", "S-PAY", "Inbound repayment instructions.", False, (
        ("instruction_id", "varchar(30)", "Instruction id"),
        ("loan_account_no", "varchar(20)", "Target loan account"),
        ("amount", "decimal(19,2)", "Amount"),
        ("value_date", "date", "Value date"),
    )),
    ("cif.customer_master", "DS-CIF", "S-CIF", "Customer golden records.", False, (
        ("cif_id", "varchar(20)", "CIF id"),
        ("external_id", "varchar(100)", "External id sent to the LSP"),
        ("date_of_birth", "date", "Date of birth"),
        ("legal_name", "varchar(200)", "Legal name"),
    )),
    ("col.collection_case", "DS-COL", "S-COL", "Collection cases.", False, (
        ("case_id", "varchar(20)", "Case id"),
        ("loan_id", "bigint", "Loan"),
        ("days_past_due", "int", "Days past due at case open"),
        ("opened_on", "date", "Opened"),
    )),
    ("clt.collateral_item", "DS-CLT", "S-CLT", "Collateral items.", False, (
        ("collateral_id", "varchar(20)", "Collateral id"),
        ("loan_id", "bigint", "Loan"),
        ("appraised_value", "decimal(19,2)", "Appraised value"),
        ("valuation_date", "date", "Valuation date"),
    )),
    ("edw.fact_loan_daily", "DS-EDW", "S-EDW", "Daily loan snapshot.", False, (
        ("loan_id", "bigint", "Loan"),
        ("cob_date", "date", "COB date"),
        ("product_code", "varchar(20)", "Product"),
        ("status_code", "int", "Loan status"),
        ("principal_outstanding", "decimal(19,2)", "Principal outstanding"),
        ("total_outstanding", "decimal(19,2)", "Total outstanding"),
        ("call_report_category", "varchar(10)", "RC-C loan category"),
    )),
    ("edw.fact_loan_txn_daily", "DS-EDW", "S-EDW", "Daily loan transactions.", False, (
        ("txn_id", "bigint", "Transaction"),
        ("loan_id", "bigint", "Loan"),
        ("cob_date", "date", "COB date"),
        ("txn_type", "varchar(20)", "Type"),
        ("amount", "decimal(19,2)", "Amount"),
        ("principal_portion", "decimal(19,2)", "Principal portion"),
        ("interest_portion", "decimal(19,2)", "Interest portion"),
        ("fee_portion", "decimal(19,2)", "Fee portion"),
        ("is_reversed", "boolean", "Reversed"),
    )),
    ("edw.fact_arrears_daily", "DS-EDW", "S-EDW", "Daily arrears snapshot.", False, (
        ("loan_id", "bigint", "Loan"),
        ("cob_date", "date", "COB date"),
        ("overdue_since", "date", "Overdue since"),
        ("days_past_due", "int", "Days past due"),
        ("dpd_bucket", "varchar(10)", "Bucket: current, 30-89, 90+"),
        ("total_overdue", "decimal(19,2)", "Total overdue"),
        ("nonaccrual_flag", "boolean", "Nonaccrual"),
    )),
    ("edw.dim_client", "DS-EDW", "S-EDW", "Client dimension.", False, (
        ("client_id", "bigint", "Client"),
        ("external_id", "varchar(100)", "CIF id"),
        ("date_of_birth", "date", "Date of birth"),
    )),
    ("edw.fact_gl_balance_daily", "DS-EDW", "S-EDW", "Daily GL balances.", False, (
        ("gl_account", "varchar(20)", "GL account"),
        ("cob_date", "date", "COB date"),
        ("balance", "decimal(19,2)", "Balance"),
    )),
    ("edw.fact_loan_provision", "DS-EDW", "S-EDW", "Provision by product.", False, (
        ("product_code", "varchar(20)", "Product"),
        ("cob_date", "date", "COB date"),
        ("provision_amount", "decimal(19,2)", "Provision amount"),
    )),
    ("edw.feed_audit", "DS-EDW", "S-EDW", "Feed landing audit.", False, (
        ("feed_id", "varchar(10)", "Feed"),
        ("cob_date", "date", "COB date"),
        ("landed_at", "timestamp", "Landing time"),
        ("source_rows", "bigint", "Rows at source"),
        ("target_rows", "bigint", "Rows landed"),
    )),
    ("rrh.call_report_extract", "DS-RRH", "S-RRH", "Call Report (FFIEC 031) line extract.", False, (
        ("report_date", "date", "Report date"),
        ("schedule", "varchar(10)", "Schedule"),
        ("line_code", "varchar(20)", "Line"),
        ("mdrm", "varchar(8)", "MDRM code"),
        ("amount", "decimal(19,0)", "Amount (USD thousands)"),
    )),
    ("rrh.fry9c_extract", "DS-RRH", "S-RRH", "FR Y-9C line extract.", False, (
        ("report_date", "date", "Report date"),
        ("schedule", "varchar(10)", "Schedule"),
        ("line_code", "varchar(20)", "Line"),
        ("mdrm", "varchar(8)", "MDRM code"),
        ("amount", "decimal(19,0)", "Amount (USD thousands)"),
    )),
)


def _tables() -> tuple[DatabaseTable, ...]:
    out = []
    for key, sid, sysid, desc, fin, cols in _TABLE_SPECS:
        name = key.split(".")[-1]
        out.append(DatabaseTable(
            key, name, sid, sysid, desc, fin, key in CODE_GRAPH_TABLES,
            tuple(DatabaseColumn(f"{key}.{c}", key, c, t, d) for c, t, d in cols)))
    return tuple(out)


FEEDS: tuple[Feed, ...] = (
    Feed("F-01", "LSP to EDW loans, daily", "S-LSP", "S-EDW",
         ("m_loan", "m_loan_repayment_schedule", "m_loan_charge"), ("edw.fact_loan_daily",),
         "daily (business days)", "lands by 06:00 ET", "OWN-DATAPLAT"),
    Feed("F-02", "LSP to EDW transactions, daily", "S-LSP", "S-EDW", ("m_loan_transaction",),
         ("edw.fact_loan_txn_daily",), "daily (business days)", "lands by 06:00 ET",
         "OWN-DATAPLAT"),
    Feed("F-03", "LSP to GL journals, intraday", "S-LSP", "S-GL", ("acc_gl_journal_entry",),
         ("gl.ledger_balance",), "hourly 07:00-20:00 ET", "within 60 minutes", "OWN-FIN"),
    Feed("F-04", "CIF to LSP client sync, daily", "S-CIF", "S-LSP", ("cif.customer_master",),
         ("m_client",), "daily", "lands by 04:00 ET", "OWN-CUSTDATA"),
    Feed("F-05", "EDW to RRH Call Report extract, quarterly", "S-EDW", "S-RRH",
         ("edw.fact_loan_daily", "edw.fact_loan_txn_daily", "edw.fact_arrears_daily",
          "edw.fact_loan_provision"), ("rrh.call_report_extract",), "quarterly",
         "quarter-end + 5 business days", "OWN-REGREP"),
    Feed("F-06", "EDW to RRH FR Y-9C extract, quarterly", "S-EDW", "S-RRH",
         ("edw.fact_loan_daily", "edw.fact_arrears_daily", "edw.fact_loan_provision"),
         ("rrh.fry9c_extract",), "quarterly", "quarter-end + 5 business days", "OWN-REGREP"),
    Feed("F-07", "Collections to EDW arrears, daily", "S-COL", "S-EDW",
         ("m_loan_arrears_aging", "col.collection_case"), ("edw.fact_arrears_daily",),
         "daily (business days)", "lands by 05:30 ET", "OWN-COLL"),
    Feed("F-08", "LSP to EDW provisioning, monthly", "S-LSP", "S-EDW",
         ("m_loanproduct_provisioning_entry",), ("edw.fact_loan_provision",), "monthly",
         "month-end + 2 business days", "OWN-FIN"),
    Feed("F-09", "Payments hub to LSP repayments, intraday", "S-PAY", "S-LSP",
         ("pay.payment_instruction",), ("m_loan_transaction",), "every 15 minutes",
         "within 30 minutes", "OWN-PAYOPS"),
    Feed("F-10", "GL to EDW balances, daily", "S-GL", "S-EDW", ("gl.ledger_balance",),
         ("edw.fact_gl_balance_daily",), "daily (business days)", "lands by 06:30 ET",
         "OWN-DATAPLAT"),
    Feed("F-11", "LSP to EDW clients, daily", "S-LSP", "S-EDW", ("m_client",),
         ("edw.dim_client",), "daily", "lands by 06:00 ET", "OWN-DATAPLAT"),
    Feed("F-12", "Collateral to EDW valuations, weekly", "S-CLT", "S-EDW",
         ("clt.collateral_item",), ("edw.fact_loan_daily",), "weekly", "Monday 06:00 ET",
         "OWN-LOANOPS"),
)

# ---------------------------------------------------------------------------------------------
# Regulatory reports, lines, MDRM (illustrative)
# ---------------------------------------------------------------------------------------------
REPORTS: tuple[RegulatoryReport, ...] = (
    RegulatoryReport("FFIEC-031", "Consolidated Reports of Condition and Income (Call Report)",
                     "FFIEC 031", "quarterly", "FFIEC (FDIC, FRB, OCC)"),
    RegulatoryReport("FR-Y-9C", "Consolidated Financial Statements for Holding Companies",
                     "FR Y-9C", "quarterly", "Federal Reserve"),
)

_LINES = (
    # line id, report, schedule, item, caption, MDRM
    ("CR-RCC-1C1", "FFIEC-031", "RC-C Part I", "1.c.(1)",
     "Revolving, open-end loans secured by 1-4 family residential properties", "RCON1797"),
    ("CR-RCC-1C2A", "FFIEC-031", "RC-C Part I", "1.c.(2)(a)",
     "Closed-end loans secured by 1-4 family residential properties: first liens", "RCON5367"),
    ("CR-RCC-1C2B", "FFIEC-031", "RC-C Part I", "1.c.(2)(b)",
     "Closed-end loans secured by 1-4 family residential properties: junior liens", "RCON5368"),
    ("CR-RCC-6C", "FFIEC-031", "RC-C Part I", "6.c", "Automobile loans", "RCONK137"),
    ("CR-RCC-6D", "FFIEC-031", "RC-C Part I", "6.d", "Other consumer loans", "RCONK207"),
    ("CR-RCC-12", "FFIEC-031", "RC-C Part I", "12",
     "Total loans and leases, net of unearned income", "RCON2122"),
    ("CR-RC-4C", "FFIEC-031", "RC", "4.c", "Allowance for credit losses on loans and leases",
     "RCON3123"),
    ("CR-RC-12", "FFIEC-031", "RC", "12", "Total assets", "RCON2170"),
    ("CR-RCN-9-30", "FFIEC-031", "RC-N", "9", "Total loans: past due 30 through 89 days",
     "RCON1406"),
    ("CR-RCN-9-90", "FFIEC-031", "RC-N", "9", "Total loans: past due 90 days or more",
     "RCON1407"),
    ("CR-RCN-9-NA", "FFIEC-031", "RC-N", "9", "Total loans: nonaccrual", "RCON1403"),
    ("CR-RIB-9-CO", "FFIEC-031", "RI-B Part I", "9", "Total charge-offs", "RIAD4635"),
    ("CR-RIB-9-RE", "FFIEC-031", "RI-B Part I", "9", "Total recoveries", "RIAD4605"),
    ("CR-RI-1A", "FFIEC-031", "RI", "1.a", "Interest and fee income on loans", "RIAD4010"),
    ("Y9C-HCC-12", "FR-Y-9C", "HC-C", "12", "Total loans and leases, net of unearned income",
     "BHCK2122"),
    ("Y9C-HC-4C", "FR-Y-9C", "HC", "4.c", "Allowance for loan and lease losses", "BHCK3123"),
    ("Y9C-HCN-9-30", "FR-Y-9C", "HC-N", "9", "Total loans: past due 30 through 89 days",
     "BHCK1406"),
    ("Y9C-HCN-9-90", "FR-Y-9C", "HC-N", "9", "Total loans: past due 90 days or more", "BHCK1407"),
    ("Y9C-HCN-9-NA", "FR-Y-9C", "HC-N", "9", "Total loans: nonaccrual", "BHCK1403"),
    ("Y9C-HIB-9-CO", "FR-Y-9C", "HI-B Part I", "9", "Total charge-offs", "BHCK4635"),
    ("Y9C-HIB-9-RE", "FR-Y-9C", "HI-B Part I", "9", "Total recoveries", "BHCK4605"),
)
REPORT_LINES: tuple[ReportLine, ...] = tuple(ReportLine(*row) for row in _LINES)
MDRMS: tuple[Mdrm, ...] = tuple(
    Mdrm(line.mdrm, line.mdrm[:4], line.mdrm[4:], line.caption, line.report_id)
    for line in REPORT_LINES)

# ---------------------------------------------------------------------------------------------
# Lineage (column level). The FR Y-9C hop EDW -> rrh.fry9c_extract is deliberately MISSING
# (examination finding EF-2025-02, issue ISS-044): the insufficient-evidence case.
# ---------------------------------------------------------------------------------------------
_CR_LINES = [ln.line_id for ln in REPORT_LINES if ln.report_id == "FFIEC-031"]
_Y9_LINES = [ln.line_id for ln in REPORT_LINES if ln.report_id == "FR-Y-9C"]
LINEAGE: tuple[Lineage, ...] = (
    Lineage("LIN-01", "m_loan.principal_outstanding_derived", "edw.fact_loan_daily.principal_outstanding",
            "database_column", "F-01", "copied as at COB"),
    Lineage("LIN-02", "edw.fact_loan_daily.principal_outstanding", "rrh.call_report_extract.amount",
            "database_column", "F-05", "summed by call_report_category, net of unearned income, "
            "in USD thousands"),
    Lineage("LIN-03", "m_loan.total_outstanding_derived", "edw.fact_loan_daily.total_outstanding",
            "database_column", "F-01", "copied as at COB"),
    Lineage("LIN-04", "m_loan_transaction.amount", "edw.fact_loan_txn_daily.amount",
            "database_column", "F-02", "copied; reversed transactions flagged"),
    Lineage("LIN-05", "m_loan_transaction.principal_portion_derived",
            "edw.fact_loan_txn_daily.principal_portion", "database_column", "F-02", "copied"),
    Lineage("LIN-06", "m_loan_transaction.interest_portion_derived",
            "edw.fact_loan_txn_daily.interest_portion", "database_column", "F-02", "copied"),
    Lineage("LIN-07", "m_loan_transaction.fee_charges_portion_derived",
            "edw.fact_loan_txn_daily.fee_portion", "database_column", "F-02", "copied"),
    Lineage("LIN-08", "edw.fact_loan_txn_daily.principal_portion", "rrh.call_report_extract.amount",
            "database_column", "F-05", "charge-offs and recoveries summed for the quarter"),
    Lineage("LIN-09", "edw.fact_loan_txn_daily.interest_portion", "rrh.call_report_extract.amount",
            "database_column", "F-05", "interest and fee income summed for the quarter"),
    Lineage("LIN-10", "m_loan_arrears_aging.overdue_since_date_derived",
            "edw.fact_arrears_daily.overdue_since", "database_column", "F-07", "copied"),
    Lineage("LIN-11", "m_loan_arrears_aging.total_overdue_derived",
            "edw.fact_arrears_daily.total_overdue", "database_column", "F-07", "copied"),
    Lineage("LIN-12", "edw.fact_arrears_daily.total_overdue", "rrh.call_report_extract.amount",
            "database_column", "F-05", "summed by dpd_bucket and nonaccrual_flag"),
    Lineage("LIN-13", "acc_gl_journal_entry.amount", "gl.ledger_balance.balance",
            "database_column", "F-03", "posted and summed by GL account"),
    Lineage("LIN-14", "gl.ledger_balance.balance", "edw.fact_gl_balance_daily.balance",
            "database_column", "F-10", "copied as at COB"),
    Lineage("LIN-15", "cif.customer_master.external_id", "m_client.external_id",
            "database_column", "F-04", "copied"),
    Lineage("LIN-16", "m_client.external_id", "edw.dim_client.external_id",
            "database_column", "F-11", "copied"),
    Lineage("LIN-17", "m_loanproduct_provisioning_entry.reseve_amount",
            "edw.fact_loan_provision.provision_amount", "database_column", "F-08",
            "summed by product"),
    Lineage("LIN-18", "edw.fact_loan_provision.provision_amount", "rrh.call_report_extract.amount",
            "database_column", "F-05", "allowance total"),
    Lineage("LIN-19", "pay.payment_instruction.amount", "m_loan_transaction.amount",
            "database_column", "F-09", "posted as a repayment"),
    Lineage("LIN-20", "cif.customer_master.date_of_birth", "m_client.date_of_birth",
            "database_column", "F-04", "copied"),
) + tuple(
    Lineage(f"LIN-CR-{i:02d}", "rrh.call_report_extract.amount", line_id, "report_line", None,
            "filed on the line")
    for i, line_id in enumerate(_CR_LINES, 1)
) + tuple(
    Lineage(f"LIN-Y9-{i:02d}", "rrh.fry9c_extract.amount", line_id, "report_line", None,
            "filed on the line")
    for i, line_id in enumerate(_Y9_LINES, 1)
)
#: Hops that exist in reality but are not registered; enrichment must report them as gaps.
KNOWN_LINEAGE_GAPS = (
    {"from": "edw.fact_loan_daily.principal_outstanding", "to": "rrh.fry9c_extract.amount",
     "feed_id": "F-06", "finding_id": "EF-2025-02", "issue_id": "ISS-044"},
)

# ---------------------------------------------------------------------------------------------
# CDEs (24)
# ---------------------------------------------------------------------------------------------
_CDE_ROWS = (
    ("CDE-LN-001", "Loan principal outstanding",
     "Unpaid principal of a loan at COB: disbursed less repaid less written off.",
     ("m_loan.principal_outstanding_derived",), "P-MFELD", ("CDE-LN-003", "CDE-TX-002"),
     ("CR-RCC-12", "Y9C-HCC-12", "CR-RCC-1C1", "CR-RCC-1C2A", "CR-RCC-1C2B", "CR-RCC-6C",
      "CR-RCC-6D")),
    ("CDE-LN-002", "Loan total outstanding",
     "Principal, interest, fees and penalties outstanding on a loan at COB.",
     ("m_loan.total_outstanding_derived",), "P-MFELD", ("CDE-LN-001",), ("CR-RC-12",)),
    ("CDE-LN-003", "Original principal", "Principal approved at origination.",
     ("m_loan.principal_amount",), "P-MFELD", (), ()),
    ("CDE-LN-004", "Loan status", "Lifecycle status of the loan (active, closed, written off).",
     ("m_loan.loan_status_id",), "P-MFELD", (), ("CR-RCN-9-NA", "Y9C-HCN-9-NA")),
    ("CDE-LN-005", "Disbursement date", "Date the loan funds were disbursed.",
     ("m_loan.disbursedon_date",), "P-MFELD", (), ()),
    ("CDE-LN-006", "Maturity date", "Contractual final repayment date.",
     ("m_loan.maturedon_date",), "P-MFELD", (), ()),
    ("CDE-LN-007", "Nominal interest rate", "Annual nominal interest rate of the loan.",
     ("m_loan.annual_nominal_interest_rate",), "P-MFELD", (), ("CR-RI-1A",)),
    ("CDE-LN-008", "Loan currency", "ISO 4217 currency of the loan.",
     ("m_loan.currency_code",), "P-MFELD", (), ()),
    ("CDE-TX-001", "Transaction amount", "Amount of a loan transaction.",
     ("m_loan_transaction.amount",), "P-ARAHMAN", (), ("CR-RIB-9-RE", "Y9C-HIB-9-RE")),
    ("CDE-TX-002", "Principal portion", "Part of a transaction allocated to principal.",
     ("m_loan_transaction.principal_portion_derived",), "P-ARAHMAN", ("CDE-TX-001",),
     ("CR-RIB-9-CO", "Y9C-HIB-9-CO")),
    ("CDE-TX-003", "Interest portion", "Part of a transaction allocated to interest.",
     ("m_loan_transaction.interest_portion_derived",), "P-ARAHMAN", ("CDE-TX-001",),
     ("CR-RI-1A",)),
    ("CDE-TX-004", "Fee portion", "Part of a transaction allocated to fees.",
     ("m_loan_transaction.fee_charges_portion_derived",), "P-ARAHMAN", ("CDE-TX-001",),
     ("CR-RI-1A",)),
    ("CDE-TX-005", "Overpayment portion", "Part of a transaction in excess of the amount owed.",
     ("m_loan_transaction.overpayment_portion_derived",), "P-ARAHMAN", ("CDE-TX-001",), ()),
    ("CDE-TX-006", "Transaction date", "Business date of a loan transaction.",
     ("m_loan_transaction.transaction_date",), "P-ARAHMAN", (), ()),
    ("CDE-TX-007", "Reversal flag", "Whether a loan transaction was reversed.",
     ("m_loan_transaction.is_reversed",), "P-ARAHMAN", (), ()),
    ("CDE-SC-001", "Installment due date", "Date an installment falls due.",
     ("m_loan_repayment_schedule.duedate",), "P-MFELD", (), ("CR-RCN-9-30",)),
    ("CDE-SC-002", "Installment completed", "Whether an installment is fully paid.",
     ("m_loan_repayment_schedule.completed_derived",), "P-MFELD", ("CDE-TX-001",), ()),
    ("CDE-AR-001", "Overdue-since date", "First date a loan's oldest unpaid installment was due.",
     ("m_loan_arrears_aging.overdue_since_date_derived",), "P-TLINDGREN", ("CDE-SC-001",),
     ("CR-RCN-9-30", "CR-RCN-9-90", "Y9C-HCN-9-30", "Y9C-HCN-9-90")),
    ("CDE-AR-002", "Total overdue", "Principal, interest, fees and penalties overdue.",
     ("m_loan_arrears_aging.total_overdue_derived",), "P-TLINDGREN", ("CDE-SC-002",),
     ("CR-RCN-9-30", "CR-RCN-9-90", "CR-RCN-9-NA", "Y9C-HCN-9-NA")),
    ("CDE-CH-001", "Charge outstanding", "Unpaid amount of a loan charge.",
     ("m_loan_charge.amount_outstanding_derived",), "P-MFELD", (), ()),
    ("CDE-CL-001", "Client external id", "The Customer Master id of a client.",
     ("m_client.external_id",), "P-PNATARAJAN", (), ()),
    ("CDE-CL-002", "Client date of birth", "Date of birth of a retail client.",
     ("m_client.date_of_birth",), "P-PNATARAJAN", (), ()),
    ("CDE-GL-001", "Journal amount", "Amount of a loan accounting journal entry.",
     ("acc_gl_journal_entry.amount",), "P-HOKAFOR", ("CDE-TX-001",), ("CR-RC-12",)),
    ("CDE-PV-001", "Provision amount", "Allowance for credit losses by product and category.",
     ("m_loanproduct_provisioning_entry.reseve_amount",), "P-HOKAFOR", ("CDE-AR-001",),
     ("CR-RC-4C", "Y9C-HC-4C")),
)
CDES: tuple[Cde, ...] = tuple(
    Cde(cid, name, d, "S-LSP", cols, steward, up, rep)
    for cid, name, d, cols, steward, up, rep in _CDE_ROWS)

# ---------------------------------------------------------------------------------------------
# DQ rules (96 active + 4 retired)
# ---------------------------------------------------------------------------------------------
#: family -> (name, primary table, owner, feed, (plural population, singular), monitor id base)
FAMILIES: dict[str, tuple[str, str, str, str | None, tuple[str, str], int]] = {
    "LN": ("Loan master", "m_loan", "OWN-LOANOPS", "F-01", ("active loans", "active loan"), 585),
    "TX": ("Loan transactions", "m_loan_transaction", "OWN-PAYOPS", "F-02",
           ("loan transactions posted on the COB date", "loan transaction"), 605),
    "SC": ("Repayment schedule", "m_loan_repayment_schedule", "OWN-LOANOPS", "F-01",
           ("open installments", "open installment"), 625),
    "AR": ("Arrears", "m_loan_arrears_aging", "OWN-COLL", "F-07",
           ("loans in arrears", "loan in arrears"), 645),
    "CH": ("Loan charges", "m_loan_charge", "OWN-LOANOPS", "F-01",
           ("active loan charges", "active loan charge"), 665),
    "CL": ("Customer", "m_client", "OWN-CUSTDATA", "F-04", ("active clients", "active client"), 685),
    "GL": ("Ledger", "acc_gl_journal_entry", "OWN-FIN", "F-03",
           ("journal entries posted on the COB date", "journal entry"), 705),
    "RP": ("Regulatory reporting", "rrh.call_report_extract", "OWN-REGREP", "F-05",
           ("report lines in the extract", "report line"), 725),
    "FD": ("Feeds", "edw.feed_audit", "OWN-DATAPLAT", None, ("feed deliveries", "feed delivery"),
           745),
}
FAMILY_ORDER = tuple(FAMILIES)
FAMILY_STEWARD = {"LN": "P-MFELD", "TX": "P-ARAHMAN", "SC": "P-MFELD", "AR": "P-TLINDGREN",
                  "CH": "P-MFELD", "CL": "P-PNATARAJAN", "GL": "P-HOKAFOR", "RP": "P-VOSEI",
                  "FD": "P-JKIM"}

# Threshold shorthands: (metric, warning, error, extra error clause)
def _rate(w, e, also=""):
    return ("rate", w, e, also)


def _count(w, e, also=""):
    return ("count", w, e, also)


def _rel(w, e):
    return ("relative_diff", w, e, "")


def _usd(w, e):
    return ("amount_usd", w, e, "")


def _mins(w, e):
    return ("minutes", w, e, "")


# Each row: (family, n, version, title, dimension, cdes, columns, statement, check, thresholds,
#            report lines, related, extra) — extra may hold legal_reference / feed / tables.
_RULE_ROWS: tuple[tuple, ...] = (
    # ---- LN: loan master (18) ----
    ("LN", 1, 1, "Principal outstanding is never negative", "validity", ("CDE-LN-001",),
     ("m_loan.principal_outstanding_derived",),
     "No loan may show a negative principal balance.",
     "For every active loan, principal_outstanding_derived is zero or greater.",
     _count(0, 5), ("CR-RCC-12", "Y9C-HCC-12"), ("NPB-DQ-LN-003",), {}),
    ("LN", 2, 1, "Principal outstanding does not exceed original principal", "validity",
     ("CDE-LN-001", "CDE-LN-003"),
     ("m_loan.principal_outstanding_derived", "m_loan.principal_amount"),
     "A loan never owes more principal than was lent, plus any capitalized amounts.",
     "For every active loan, principal outstanding is at most original principal plus capitalized "
     "interest and fees.", _rate(0.0001, 0.001), ("CR-RCC-12",), ("NPB-DQ-LN-003",), {}),
    ("LN", 3, 2, "Principal outstanding reconciles to its components", "accuracy", ("CDE-LN-001",),
     ("m_loan.principal_outstanding_derived", "m_loan.principal_disbursed_derived",
      "m_loan.principal_repaid_derived", "m_loan.principal_writtenoff_derived"),
     "The loan balance the bank reports must equal what was disbursed, less what was repaid and "
     "written off.",
     "For every active loan, principal outstanding equals principal disbursed minus principal "
     "repaid minus principal written off, to the cent.",
     _rate(0.0005, 0.0025, "any single loan differs by more than USD 100"),
     ("CR-RCC-12", "Y9C-HCC-12"), ("NPB-DQ-GL-002", "NPB-DQ-TX-006"), {}),
    ("LN", 4, 1, "Total outstanding equals the sum of its parts", "accuracy", ("CDE-LN-002",),
     ("m_loan.total_outstanding_derived", "m_loan.principal_outstanding_derived",
      "m_loan.interest_outstanding_derived", "m_loan.fee_charges_outstanding_derived",
      "m_loan.penalty_charges_outstanding_derived"),
     "Total outstanding is principal plus interest plus fees plus penalties outstanding.",
     "For every active loan, total_outstanding_derived equals the sum of the principal, interest, "
     "fee and penalty outstanding columns.", _rate(0.0005, 0.0025), ("CR-RC-12",),
     ("NPB-DQ-LN-003",), {}),
    ("LN", 5, 1, "A closed loan carries zero outstanding", "consistency",
     ("CDE-LN-002", "CDE-LN-004"), ("m_loan.total_outstanding_derived", "m_loan.loan_status_id"),
     "A loan closed with obligations met owes nothing.",
     "For every loan in status 600 (closed, obligations met), total outstanding is zero.",
     _count(0, 10), (), ("NPB-DQ-LN-015",), {}),
    ("LN", 6, 1, "Maturity is after disbursement", "validity", ("CDE-LN-005", "CDE-LN-006"),
     ("m_loan.disbursedon_date", "m_loan.maturedon_date"),
     "A loan cannot mature before it is disbursed.",
     "For every disbursed loan, maturedon_date is later than disbursedon_date.",
     _count(0, 5), (), ("NPB-DQ-SC-006",), {}),
    ("LN", 7, 1, "Disbursement date is present for disbursed loans", "completeness",
     ("CDE-LN-005",), ("m_loan.disbursedon_date", "m_loan.loan_status_id"),
     "Every loan that has been disbursed records when.",
     "For every loan in status 300 or later, disbursedon_date is not null.",
     _rate(0.0001, 0.001), (), (), {}),
    ("LN", 8, 1, "Nominal rate is within the product's range", "validity", ("CDE-LN-007",),
     ("m_loan.annual_nominal_interest_rate", "m_product_loan.min_nominal_interest_rate_per_period",
      "m_product_loan.max_nominal_interest_rate_per_period"),
     "A loan's interest rate stays within what its product allows.",
     "For every active loan, the nominal rate lies between the product's minimum and maximum "
     "nominal rate.", _rate(0.0005, 0.005), ("CR-RI-1A",), (), {}),
    ("LN", 9, 1, "Currency is a valid ISO 4217 code", "validity", ("CDE-LN-008",),
     ("m_loan.currency_code", "organisationcurrency.code"),
     "Every loan is denominated in a currency the bank has enabled.",
     "For every active loan, currency_code is an ISO 4217 code present in organisationcurrency.",
     _count(0, 1), (), (), {}),
    ("LN", 10, 1, "Loan status is a recognised status", "validity", ("CDE-LN-004",),
     ("m_loan.loan_status_id",),
     "Every loan carries one of the statuses the platform defines.",
     "For every loan, loan_status_id is one of 100, 200, 300, 400, 500, 600, 601, 602 or 700.",
     _count(0, 1), (), (), {}),
    ("LN", 11, 1, "Active loans have been disbursed", "consistency",
     ("CDE-LN-004", "CDE-LN-005"), ("m_loan.loan_status_id", "m_loan.principal_disbursed_derived"),
     "A loan is active only once money has gone out.",
     "For every loan in status 300 (active), principal disbursed is greater than zero.",
     _count(0, 5), (), ("NPB-DQ-LN-007",), {}),
    ("LN", 12, 1, "Original principal is within the product's range", "validity", ("CDE-LN-003",),
     ("m_loan.principal_amount", "m_product_loan.min_principal_amount",
      "m_product_loan.max_principal_amount"),
     "Each loan's approved principal respects its product limits.",
     "For every active loan, principal_amount lies between the product's minimum and maximum "
     "principal.", _rate(0.0005, 0.005), (), (), {}),
    ("LN", 13, 1, "Principal outstanding is populated", "completeness", ("CDE-LN-001",),
     ("m_loan.principal_outstanding_derived",),
     "Every active loan has a principal balance recorded.",
     "For every active loan, principal_outstanding_derived is not null.",
     _rate(0.0001, 0.001), ("CR-RCC-12",), ("NPB-DQ-LN-001",), {}),
    ("LN", 14, 1, "Derived balances are refreshed at COB", "timeliness", ("CDE-LN-002",),
     ("m_loan.last_modified_on_utc", "m_loan.total_outstanding_derived"),
     "Balances are recomputed after the last transaction of the day, before the warehouse "
     "extract.",
     "For every active loan with a transaction on the COB date, last_modified_on_utc is later "
     "than that transaction and earlier than the F-01 extract start.",
     _rate(0.001, 0.005), (), ("NPB-DQ-LN-003", "NPB-DQ-FD-001"), {}),
    ("LN", 15, 1, "Written-off loans carry a write-off date and no principal", "consistency",
     ("CDE-LN-001", "CDE-LN-004"),
     ("m_loan.writtenoffon_date", "m_loan.principal_outstanding_derived", "m_loan.loan_status_id"),
     "A written-off loan says when, and carries no principal balance.",
     "For every loan in status 601 (written off), writtenoffon_date is not null and principal "
     "outstanding is zero.", _count(0, 3), ("CR-RIB-9-CO",), ("NPB-DQ-TX-017",), {}),
    ("LN", 16, 1, "Loan account number is unique", "uniqueness", ("CDE-LN-001",),
     ("m_loan.account_no",),
     "No two loans share an account number, so balances are never double counted.",
     "Across all loans, account_no has no duplicates.", _count(0, 1), (), (), {}),
    ("LN", 17, 1, "Every loan belongs to an existing client", "consistency", ("CDE-LN-004",),
     ("m_loan.client_id", "m_client.id"),
     "A loan always points at a real borrower.",
     "For every loan, client_id matches an id in m_client.", _count(0, 1), (),
     ("NPB-DQ-CL-005",), {}),
    ("LN", 18, 1, "Every loan maps to a Call Report loan category", "completeness",
     ("CDE-LN-001",), ("edw.fact_loan_daily.call_report_category",),
     "Every loan lands in exactly one RC-C category, so the schedule adds up.",
     "For every active loan in the EDW snapshot, call_report_category is populated with a valid "
     "RC-C category.", _rate(0.0001, 0.0005),
     ("CR-RCC-1C1", "CR-RCC-1C2A", "CR-RCC-1C2B", "CR-RCC-6C", "CR-RCC-6D"), ("NPB-DQ-RP-002",),
     {"system": "S-EDW"}),
    # ---- TX: transactions (18) ----
    ("TX", 1, 1, "Transaction amount is positive", "validity", ("CDE-TX-001",),
     ("m_loan_transaction.amount",), "Money moves in positive amounts; direction comes from the "
     "type.", "For every loan transaction, amount is greater than zero.", _count(0, 5), (), (), {}),
    ("TX", 2, 1, "Transaction date is not after COB", "validity", ("CDE-TX-006",),
     ("m_loan_transaction.transaction_date",), "Nothing is booked in the future.",
     "For every loan transaction, transaction_date is on or before the COB date.",
     _count(0, 1), (), (), {}),
    ("TX", 3, 1, "Transaction date is not before disbursement", "consistency",
     ("CDE-TX-006", "CDE-LN-005"), ("m_loan_transaction.transaction_date", "m_loan.disbursedon_date"),
     "Repayments cannot precede the loan.",
     "For every non-disbursement loan transaction, transaction_date is on or after the loan's "
     "disbursedon_date.", _count(0, 3), (), (), {}),
    ("TX", 4, 1, "Reversed transactions are excluded from balances", "consistency",
     ("CDE-TX-007", "CDE-LN-001"),
     ("m_loan_transaction.is_reversed", "m_loan.principal_outstanding_derived"),
     "A reversed payment no longer reduces what the borrower owes.",
     "For every loan, principal outstanding recomputed from non-reversed transactions equals "
     "principal_outstanding_derived.", _rate(0.0005, 0.0025), ("CR-RCC-12",),
     ("NPB-DQ-LN-003",), {}),
    ("TX", 5, 1, "A reversed transaction carries a reversal date", "completeness", ("CDE-TX-007",),
     ("m_loan_transaction.is_reversed", "m_loan_transaction.reversed_on_date"),
     "When a transaction is reversed the bank records when.",
     "For every loan transaction with is_reversed true, reversed_on_date is not null.",
     _rate(0.0005, 0.005), (), (), {}),
    ("TX", 6, 1, "Transaction amount equals the sum of its portions", "accuracy",
     ("CDE-TX-001", "CDE-TX-002", "CDE-TX-003", "CDE-TX-004", "CDE-TX-005"),
     ("m_loan_transaction.amount", "m_loan_transaction.principal_portion_derived",
      "m_loan_transaction.interest_portion_derived", "m_loan_transaction.fee_charges_portion_derived",
      "m_loan_transaction.penalty_charges_portion_derived",
      "m_loan_transaction.overpayment_portion_derived"),
     "Every cent of a payment is allocated somewhere.",
     "For every non-reversed loan transaction, amount equals principal plus interest plus fee plus "
     "penalty plus overpayment portions, to the cent.", _rate(0.0001, 0.001), ("CR-RI-1A",),
     ("NPB-DQ-LN-003",), {}),
    ("TX", 7, 1, "No duplicate external reference", "uniqueness", ("CDE-TX-001",),
     ("m_loan_transaction.external_id",), "A payment from the hub is posted once.",
     "Across non-reversed loan transactions, external_id has no duplicates.", _count(0, 1), (),
     (), {}),
    ("TX", 8, 1, "Repayment portions never exceed what was due", "accuracy",
     ("CDE-TX-002", "CDE-TX-003"),
     ("m_loan_transaction.principal_portion_derived", "m_loan_transaction.interest_portion_derived"),
     "A repayment cannot pay more principal or interest than the loan owed.",
     "For every repayment, the principal and interest portions are at most the principal and "
     "interest due before the transaction.", _rate(0.0001, 0.001), (), (), {}),
    ("TX", 9, 2, "A short payment is held in suspense until a full periodic payment",
     "consistency", ("CDE-TX-001", "CDE-SC-002"),
     ("m_loan_transaction.amount", "m_loan_repayment_schedule.completed_derived"),
     "A payment less than a periodic payment is not credited to the installment; it is held and "
     "credited once accumulated funds reach a full periodic payment.",
     "For every repayment smaller than the installment due, the amount is held in suspense and "
     "credited when accumulated suspense reaches a full periodic payment.",
     _rate(0.0001, 0.001), ("CR-RCN-9-30",), ("NPB-DQ-TX-010", "NPB-DQ-SC-004"),
     {"legal": "12 CFR 1026.36(c)(1)(ii) (Regulation Z, partial payments)"}),
    ("TX", 10, 1, "A short payment never completes an installment", "consistency",
     ("CDE-TX-001", "CDE-SC-002"),
     ("m_loan_transaction.amount", "m_loan_repayment_schedule.completed_derived"),
     "Paying part of an installment never marks it paid.",
     "For every installment paid by a payment smaller than the amount due, completed_derived "
     "stays false.", _rate(0.0001, 0.0005), ("CR-RCN-9-30",), ("NPB-DQ-TX-009", "NPB-DQ-SC-004"),
     {}),
    ("TX", 11, 1, "A chargeback posts fully to principal", "accuracy", ("CDE-TX-002",),
     ("m_loan_transaction.principal_portion_derived", "m_loan_transaction.transaction_type_enum"),
     "A card chargeback adds back principal only.",
     "For every chargeback transaction, principal portion equals the amount and other portions "
     "are zero.", _count(0, 3), (), (), {}),
    ("TX", 12, 1, "Overpayment only when the loan is fully paid", "consistency",
     ("CDE-TX-005", "CDE-LN-002"),
     ("m_loan_transaction.overpayment_portion_derived", "m_loan.total_outstanding_derived"),
     "Money is called overpayment only after everything owed is paid.",
     "For every transaction with an overpayment portion, the loan's total outstanding after the "
     "transaction is zero.", _count(0, 3), (), (), {}),
    ("TX", 13, 1, "Interest portion is never negative", "validity", ("CDE-TX-003",),
     ("m_loan_transaction.interest_portion_derived",), "Interest allocation cannot be negative.",
     "For every loan transaction, interest_portion_derived is zero or greater.", _count(0, 1),
     ("CR-RI-1A",), (), {}),
    ("TX", 14, 1, "Fee portion matches the charges paid", "consistency",
     ("CDE-TX-004", "CDE-CH-001"),
     ("m_loan_transaction.fee_charges_portion_derived", "m_loan_charge.amount_paid_derived"),
     "Fees taken from a payment show up as fees paid on the charge.",
     "For every loan, the sum of fee portions equals the sum of amount_paid_derived on its fee "
     "charges.", _rate(0.0005, 0.005), ("CR-RI-1A",), ("NPB-DQ-CH-001",), {}),
    ("TX", 15, 1, "Transaction type is recognised", "validity", ("CDE-TX-001",),
     ("m_loan_transaction.transaction_type_enum",),
     "Every transaction has a type the platform defines.",
     "For every loan transaction, transaction_type_enum is a defined loan transaction type.",
     _count(0, 1), (), (), {}),
    ("TX", 16, 1, "Transactions are posted within one business day", "timeliness",
     ("CDE-TX-006",),
     ("m_loan_transaction.transaction_date", "m_loan_transaction.submitted_on_date"),
     "Payments are booked promptly so balances are current at COB.",
     "For every loan transaction, submitted_on_date is at most one business day after "
     "transaction_date.", _rate(0.002, 0.01), (), ("NPB-DQ-FD-003",), {}),
    ("TX", 17, 1, "Charge-offs agree with the loan's write-off", "accuracy",
     ("CDE-TX-002", "CDE-LN-001"),
     ("m_loan_transaction.principal_portion_derived", "m_loan.principal_writtenoff_derived"),
     "What the bank charges off equals what the loan shows written off.",
     "For every written-off loan, the principal portion of its write-off transactions equals "
     "principal_writtenoff_derived.", _count(0, 2), ("CR-RIB-9-CO", "Y9C-HIB-9-CO"),
     ("NPB-DQ-RP-003",), {}),
    ("TX", 18, 1, "Recoveries post only to charged-off loans", "consistency",
     ("CDE-TX-001", "CDE-LN-004"),
     ("m_loan_transaction.transaction_type_enum", "m_loan.loan_status_id"),
     "A recovery is money collected after charge-off.",
     "For every recovery transaction, the loan is in status 601 (written off).", _count(0, 2),
     ("CR-RIB-9-RE", "Y9C-HIB-9-RE"), ("NPB-DQ-RP-004",), {}),
    # ---- SC: schedule (10) ----
    ("SC", 1, 1, "Installment numbers are contiguous", "completeness", ("CDE-SC-001",),
     ("m_loan_repayment_schedule.installment",), "A schedule has no missing installments.",
     "For every loan, installment numbers run 1, 2, 3 ... with no gaps.", _count(0, 5), (), (),
     {}),
    ("SC", 2, 1, "Due dates rise strictly", "validity", ("CDE-SC-001",),
     ("m_loan_repayment_schedule.duedate", "m_loan_repayment_schedule.installment"),
     "Each installment falls due after the one before.",
     "For every loan, duedate increases strictly with installment number.", _count(0, 5), (), (),
     {}),
    ("SC", 3, 1, "Each period starts where the last ended", "consistency", ("CDE-SC-001",),
     ("m_loan_repayment_schedule.fromdate", "m_loan_repayment_schedule.duedate"),
     "Schedule periods are back to back.",
     "For every installment after the first, fromdate equals the previous installment's duedate.",
     _count(0, 5), (), (), {}),
    ("SC", 4, 1, "An installment is completed only when fully paid", "accuracy", ("CDE-SC-002",),
     ("m_loan_repayment_schedule.completed_derived",
      "m_loan_repayment_schedule.principal_completed_derived",
      "m_loan_repayment_schedule.interest_completed_derived"),
     "An installment counts as paid only when principal, interest, fees and penalties are all "
     "paid.",
     "For every installment with completed_derived true, principal, interest, fees and penalties "
     "completed equal the amounts scheduled.", _rate(0.0001, 0.0005), ("CR-RCN-9-30",),
     ("NPB-DQ-TX-010", "NPB-DQ-AR-001"), {}),
    ("SC", 5, 1, "Scheduled principal adds up to the disbursed principal", "accuracy",
     ("CDE-SC-001", "CDE-LN-003"),
     ("m_loan_repayment_schedule.principal_amount", "m_loan.principal_disbursed_derived"),
     "The schedule repays exactly what was lent.",
     "For every disbursed loan, the sum of scheduled principal equals principal disbursed.",
     _rate(0.0005, 0.0025), (), ("NPB-DQ-LN-003",), {}),
    ("SC", 6, 1, "The last installment falls on the maturity date", "consistency",
     ("CDE-SC-001", "CDE-LN-006"), ("m_loan_repayment_schedule.duedate", "m_loan.maturedon_date"),
     "The schedule ends when the loan matures.",
     "For every active loan, the last installment's duedate equals maturedon_date.",
     _rate(0.0005, 0.005), (), ("NPB-DQ-LN-006",), {}),
    ("SC", 7, 1, "Principal paid never exceeds principal scheduled", "validity", ("CDE-SC-002",),
     ("m_loan_repayment_schedule.principal_completed_derived",
      "m_loan_repayment_schedule.principal_amount"),
     "An installment cannot be overpaid in principal.",
     "For every installment, principal_completed_derived is at most principal_amount.",
     _count(0, 5), (), (), {}),
    ("SC", 8, 1, "Interest paid never exceeds interest scheduled", "validity", ("CDE-SC-002",),
     ("m_loan_repayment_schedule.interest_completed_derived",
      "m_loan_repayment_schedule.interest_amount"),
     "An installment cannot be overpaid in interest.",
     "For every installment, interest_completed_derived is at most interest_amount.",
     _count(0, 5), (), (), {}),
    ("SC", 9, 1, "Completed installments record when they were met", "completeness",
     ("CDE-SC-002",),
     ("m_loan_repayment_schedule.obligations_met_on_date",
      "m_loan_repayment_schedule.completed_derived"),
     "When an installment is paid, the date it was paid is kept.",
     "For every installment with completed_derived true, obligations_met_on_date is not null.",
     _rate(0.0005, 0.005), (), (), {}),
    ("SC", 10, 1, "No installment is due on a non-business day", "validity", ("CDE-SC-001",),
     ("m_loan_repayment_schedule.duedate",),
     "Due dates respect the bank's business calendar.",
     "For every open installment, duedate is a business day in the NorthPeak business calendar, "
     "or carries a holiday adjustment.", _rate(0.0005, 0.005), (), ("NPB-DQ-AR-001",), {}),
    # ---- AR: arrears (10) ----
    ("AR", 1, 2, "Overdue-since is set within one business day", "timeliness", ("CDE-AR-001",),
     ("m_loan_arrears_aging.overdue_since_date_derived",),
     "When an installment is missed or paid short, the loan shows as overdue by the next "
     "business day.",
     "For every loan with an installment unpaid past its due date, overdue_since_date_derived is "
     "set within one business day of that due date.", _rate(0.001, 0.005),
     ("CR-RCN-9-30", "Y9C-HCN-9-30"), ("NPB-DQ-SC-004", "NPB-DQ-FD-004"), {}),
    ("AR", 2, 1, "Overdue-since is not after COB", "validity", ("CDE-AR-001",),
     ("m_loan_arrears_aging.overdue_since_date_derived",), "A loan cannot be overdue from the "
     "future.", "For every arrears row, overdue_since_date_derived is on or before the COB date.",
     _count(0, 1), (), (), {}),
    ("AR", 3, 1, "Days-past-due bucket agrees with overdue-since", "consistency", ("CDE-AR-001",),
     ("edw.fact_arrears_daily.dpd_bucket", "edw.fact_arrears_daily.overdue_since"),
     "The past-due bucket reported is the one the overdue-since date implies.",
     "For every loan in arrears, dpd_bucket equals the bucket computed from COB date minus "
     "overdue_since.", _rate(0.0005, 0.0025),
     ("CR-RCN-9-30", "CR-RCN-9-90", "Y9C-HCN-9-30", "Y9C-HCN-9-90"), ("NPB-DQ-AR-001",),
     {"system": "S-EDW", "feed": "F-07"}),
    ("AR", 4, 1, "Total overdue never exceeds total outstanding", "validity",
     ("CDE-AR-002", "CDE-LN-002"),
     ("m_loan_arrears_aging.total_overdue_derived", "m_loan.total_outstanding_derived"),
     "A borrower cannot be behind by more than they owe.",
     "For every loan in arrears, total_overdue_derived is at most total_outstanding_derived.",
     _count(0, 3), (), (), {}),
    ("AR", 5, 1, "Total overdue equals the sum of its parts", "accuracy", ("CDE-AR-002",),
     ("m_loan_arrears_aging.total_overdue_derived", "m_loan_arrears_aging.principal_overdue_derived",
      "m_loan_arrears_aging.interest_overdue_derived",
      "m_loan_arrears_aging.fee_charges_overdue_derived",
      "m_loan_arrears_aging.penalty_charges_overdue_derived"),
     "Total overdue is principal plus interest plus fees plus penalties overdue.",
     "For every loan in arrears, total_overdue_derived equals the sum of the four overdue "
     "components.", _rate(0.0005, 0.0025), ("CR-RCN-9-30", "CR-RCN-9-90"), (), {}),
    ("AR", 6, 1, "Every loan with an overdue installment has an arrears row", "completeness",
     ("CDE-AR-001",), ("m_loan_arrears_aging.loan_id", "m_loan_repayment_schedule.duedate"),
     "No overdue loan is missing from arrears aging.",
     "For every active loan with an incomplete installment due before COB, a row exists in "
     "m_loan_arrears_aging.", _rate(0.0005, 0.0025), ("CR-RCN-9-30",), (), {}),
    ("AR", 7, 1, "No arrears row for a loan that is up to date", "consistency", ("CDE-AR-002",),
     ("m_loan_arrears_aging.loan_id", "m_loan_arrears_aging.total_overdue_derived"),
     "A current loan is not reported as past due.",
     "For every row in m_loan_arrears_aging, the loan has at least one incomplete installment due "
     "before COB.", _rate(0.0005, 0.0025), ("CR-RCN-9-30",), (), {}),
    ("AR", 8, 1, "Loans 90 or more days past due are nonaccrual", "consistency",
     ("CDE-AR-001", "CDE-LN-004"),
     ("edw.fact_arrears_daily.days_past_due", "edw.fact_arrears_daily.nonaccrual_flag"),
     "Loans 90 days or more past due stop accruing interest unless well secured and in "
     "collection.",
     "For every loan with days_past_due of 90 or more, nonaccrual_flag is true or an approved "
     "exception exists.", _rate(0.001, 0.005), ("CR-RCN-9-90", "CR-RCN-9-NA", "Y9C-HCN-9-NA"),
     ("NPB-DQ-AR-003",), {"system": "S-EDW", "feed": "F-07"}),
    ("AR", 9, 1, "Principal overdue matches the schedule", "accuracy", ("CDE-AR-002",),
     ("m_loan_arrears_aging.principal_overdue_derived",
      "m_loan_repayment_schedule.principal_amount",
      "m_loan_repayment_schedule.principal_completed_derived"),
     "Principal overdue is what the schedule says is due and unpaid.",
     "For every loan in arrears, principal_overdue_derived equals scheduled principal minus "
     "principal completed on installments due before COB.", _rate(0.0005, 0.0025), (), (), {}),
    ("AR", 10, 1, "Arrears aging is refreshed before the EDW extract", "timeliness",
     ("CDE-AR-001",), ("m_loan_arrears_aging.overdue_since_date_derived",),
     "Arrears are recomputed every business day before the warehouse reads them.",
     "On every business day, the arrears aging job completes before the F-07 extract starts.",
     _mins(0, 30), (), ("NPB-DQ-FD-004",), {}),
    # ---- CH: charges (8) ----
    ("CH", 1, 1, "Charge outstanding reconciles to its components", "accuracy", ("CDE-CH-001",),
     ("m_loan_charge.amount_outstanding_derived", "m_loan_charge.amount",
      "m_loan_charge.amount_paid_derived", "m_loan_charge.amount_waived_derived",
      "m_loan_charge.amount_writtenoff_derived"),
     "What is owed on a charge is its amount less what was paid, waived or written off.",
     "For every active loan charge, amount outstanding equals amount minus paid minus waived minus "
     "written off.", _rate(0.0001, 0.001), (), ("NPB-DQ-TX-014",), {}),
    ("CH", 2, 1, "Payments never exceed a charge's outstanding amount", "validity",
     ("CDE-CH-001",), ("m_loan_charge.amount_paid_derived", "m_loan_charge.amount"),
     "A charge cannot be overpaid.",
     "For every loan charge, amount_paid_derived is at most amount minus waived minus written "
     "off.", _count(0, 3), (), (), {}),
    ("CH", 3, 1, "Paid flag agrees with the outstanding amount", "consistency", ("CDE-CH-001",),
     ("m_loan_charge.is_paid_derived", "m_loan_charge.amount_outstanding_derived"),
     "A charge is flagged paid exactly when nothing is outstanding.",
     "For every loan charge, is_paid_derived is true if and only if amount_outstanding_derived is "
     "zero.", _rate(0.0005, 0.005), (), (), {}),
    ("CH", 4, 1, "Charge amount is positive", "validity", ("CDE-CH-001",),
     ("m_loan_charge.amount",), "A charge is always a positive amount.",
     "For every loan charge, amount is greater than zero.", _count(0, 1), (), (), {}),
    ("CH", 5, 1, "Every loan charge has a charge definition", "consistency", ("CDE-CH-001",),
     ("m_loan_charge.charge_id", "m_charge.id"), "Every fee on a loan is a fee the bank defined.",
     "For every loan charge, charge_id matches an id in m_charge.", _count(0, 1), (), (), {}),
    ("CH", 6, 1, "Specified-due-date charges have a due date", "completeness", ("CDE-CH-001",),
     ("m_loan_charge.due_for_collection_as_of_date",),
     "A fee due on a date records that date.",
     "For every active specified-due-date charge, due_for_collection_as_of_date is not null.",
     _rate(0.0005, 0.005), (), (), {}),
    ("CH", 7, 1, "Waived amount never exceeds the charge", "validity", ("CDE-CH-001",),
     ("m_loan_charge.amount_waived_derived", "m_loan_charge.amount"),
     "The bank cannot waive more than it charged.",
     "For every loan charge, amount_waived_derived is at most amount.", _count(0, 1), (), (), {}),
    ("CH", 8, 1, "Inactive charges carry nothing outstanding", "consistency", ("CDE-CH-001",),
     ("m_loan_charge.is_active", "m_loan_charge.amount_outstanding_derived"),
     "A removed charge is not still owed.",
     "For every loan charge with is_active false, amount_outstanding_derived is zero.",
     _count(0, 3), (), (), {}),
    # ---- CL: customer (10) ----
    ("CL", 1, 1, "Client external id is unique", "uniqueness", ("CDE-CL-001",),
     ("m_client.external_id",), "One person, one Customer Master id.",
     "Across active clients, external_id has no duplicates.", _count(0, 1), (),
     ("NPB-DQ-CL-009",), {}),
    ("CL", 2, 1, "Date of birth is present for retail clients", "completeness", ("CDE-CL-002",),
     ("m_client.date_of_birth", "m_client.legal_form_enum"),
     "The bank knows the age of every individual borrower.",
     "For every active client with legal_form_enum 1 (person), date_of_birth is not null.",
     _rate(0.0005, 0.005), (), ("NPB-DQ-CL-004",), {}),
    ("CL", 3, 1, "External id is present", "completeness", ("CDE-CL-001",),
     ("m_client.external_id",), "Every client links to the Customer Master.",
     "For every active client, external_id is not null.", _rate(0.0001, 0.001), (), (), {}),
    ("CL", 4, 3, "Retail client age is between 18 and 110", "validity", ("CDE-CL-002",),
     ("m_client.date_of_birth", "m_client.legal_form_enum"),
     "Individual borrowers are adults and their recorded age is plausible.",
     "For every active client with legal_form_enum 1 (person), age at COB computed from "
     "date_of_birth is between 18 and 110.", _rate(0.0002, 0.005), (), ("NPB-DQ-CL-002",), {}),
    ("CL", 5, 1, "Every loan's client exists", "consistency", ("CDE-CL-001",),
     ("m_loan.client_id", "m_client.id"), "No loan points at a missing client.",
     "For every loan, client_id matches an active or closed client in m_client.", _count(0, 1),
     (), ("NPB-DQ-LN-017",), {}),
    ("CL", 6, 1, "Activation date is not in the future", "validity", ("CDE-CL-001",),
     ("m_client.activation_date",), "A client cannot be activated after today.",
     "For every active client, activation_date is on or before the COB date.", _count(0, 1), (),
     (), {}),
    ("CL", 7, 1, "Display name is present", "completeness", ("CDE-CL-001",),
     ("m_client.display_name",), "Every client has a name.",
     "For every active client, display_name is not null or blank.", _rate(0.0001, 0.001), (), (),
     {}),
    ("CL", 8, 1, "Client status is recognised", "validity", ("CDE-CL-001",),
     ("m_client.status_enum",), "Every client has a defined status.",
     "For every client, status_enum is one of 100, 300, 600 or 700.", _count(0, 1), (), (), {}),
    ("CL", 9, 1, "External id matches the Customer Master", "consistency", ("CDE-CL-001",),
     ("m_client.external_id", "cif.customer_master.external_id"),
     "The id on the loan platform is the id in the Customer Master.",
     "For every active client, external_id exists in cif.customer_master.", _rate(0.0005, 0.005),
     (), ("NPB-DQ-CL-001",), {}),
    ("CL", 10, 1, "Legal form is set", "completeness", ("CDE-CL-002",),
     ("m_client.legal_form_enum",), "Every client is either a person or an entity.",
     "For every active client, legal_form_enum is 1 (person) or 2 (entity).",
     _rate(0.0005, 0.005), (), ("NPB-DQ-CL-002",), {}),
    # ---- GL: ledger (8) ----
    ("GL", 1, 1, "Every loan transaction has balanced journal entries", "accuracy",
     ("CDE-GL-001",), ("acc_gl_journal_entry.amount", "acc_gl_journal_entry.loan_transaction_id",
                       "acc_gl_journal_entry.type_enum"),
     "Debits equal credits for every loan transaction.",
     "For every non-reversed loan transaction, the sum of its debit journal amounts equals the sum "
     "of its credit journal amounts.", _count(0, 1), ("CR-RC-12",), (), {}),
    ("GL", 2, 2, "Principal outstanding reconciles to the GL", "accuracy",
     ("CDE-GL-001", "CDE-LN-001"),
     ("gl.ledger_balance.balance", "m_loan.principal_outstanding_derived"),
     "The loan book and the general ledger agree.",
     "For every loan product, the sum of principal outstanding reconciles to the GL "
     "loans-receivable account balance within 0.01%.", _rel(0.0001, 0.0005), ("CR-RCC-12",),
     ("NPB-DQ-LN-003", "NPB-DQ-GL-007"), {"system": "S-GL", "feed": "F-10"}),
    ("GL", 3, 1, "Journal amount is positive", "validity", ("CDE-GL-001",),
     ("acc_gl_journal_entry.amount",), "Journal amounts are positive; direction is debit or "
     "credit.", "For every journal entry, amount is greater than zero.", _count(0, 1), (), (), {}),
    ("GL", 4, 1, "Journal entries use an enabled GL account", "consistency", ("CDE-GL-001",),
     ("acc_gl_journal_entry.account_id", "acc_gl_account.disabled"),
     "Nothing posts to a closed account.",
     "For every journal entry, account_id refers to an acc_gl_account that is not disabled.",
     _count(0, 1), (), (), {}),
    ("GL", 5, 1, "Reversed transactions have reversing journals", "consistency",
     ("CDE-GL-001", "CDE-TX-007"),
     ("acc_gl_journal_entry.reversed", "m_loan_transaction.is_reversed"),
     "When a transaction is reversed, so is its accounting.",
     "For every reversed loan transaction, every journal entry it created is marked reversed.",
     _count(0, 3), (), ("NPB-DQ-TX-004",), {}),
    ("GL", 6, 1, "Journal date equals the transaction date", "consistency",
     ("CDE-GL-001", "CDE-TX-006"),
     ("acc_gl_journal_entry.entry_date", "m_loan_transaction.transaction_date"),
     "Accounting lands on the day the business event happened.",
     "For every journal entry with a loan_transaction_id, entry_date equals that transaction's "
     "transaction_date.", _rate(0.0005, 0.005), (), (), {}),
    ("GL", 7, 1, "Every loan product is mapped to GL accounts", "completeness", ("CDE-GL-001",),
     ("acc_product_mapping.gl_account_id", "acc_product_mapping.product_id"),
     "Every product knows where its accounting goes.",
     "For every active loan product, acc_product_mapping holds a GL account for each required "
     "financial account type.", _count(0, 0), ("CR-RCC-12",), ("NPB-DQ-GL-002",),
     {"population": ("active loan products", "active loan product")}),
    ("GL", 8, 1, "Provision expense agrees with the provision change", "accuracy",
     ("CDE-PV-001", "CDE-GL-001"),
     ("m_loanproduct_provisioning_entry.reseve_amount", "acc_gl_journal_entry.amount"),
     "The provision booked in the ledger matches the provisioning run.",
     "For every provisioning run, the provision expense journals equal the change in provision "
     "amount since the previous run, within USD 1.", _count(0, 0), ("CR-RC-4C", "Y9C-HC-4C"),
     ("NPB-DQ-RP-007",), {"population": ("provisioning runs", "provisioning run")}),
    # ---- RP: regulatory reporting (10) ----
    ("RP", 1, 2, "RCON2122 ties to the EDW loan total", "accuracy", ("CDE-LN-001",),
     ("rrh.call_report_extract.amount", "edw.fact_loan_daily.principal_outstanding"),
     "Total loans on the Call Report equal the warehouse total of loans net of unearned income.",
     "At each quarter-end, RC-C item 12 (RCON2122) equals the EDW total of loans net of unearned "
     "income within USD 1,000.", _usd(1000, 25000), ("CR-RCC-12",),
     ("NPB-DQ-LN-003", "NPB-DQ-GL-002"), {}),
    ("RP", 2, 1, "RC-C categories add up to total loans", "consistency", ("CDE-LN-001",),
     ("rrh.call_report_extract.amount",),
     "The RC-C category lines add up to the total reported.",
     "At each quarter-end, the sum of RC-C category lines equals RC-C item 12.", _usd(0, 1000),
     ("CR-RCC-12", "CR-RCC-1C1", "CR-RCC-1C2A", "CR-RCC-1C2B", "CR-RCC-6C", "CR-RCC-6D"),
     ("NPB-DQ-LN-018",), {}),
    ("RP", 3, 1, "Charge-offs agree with charge-off transactions", "accuracy", ("CDE-TX-002",),
     ("rrh.call_report_extract.amount", "edw.fact_loan_txn_daily.principal_portion"),
     "Charge-offs reported equal the charge-off transactions in the quarter.",
     "For the quarter, RI-B total charge-offs (RIAD4635) equals the sum of charge-off principal "
     "in the EDW within USD 1,000.", _usd(1000, 10000), ("CR-RIB-9-CO", "Y9C-HIB-9-CO"),
     ("NPB-DQ-TX-017",), {}),
    ("RP", 4, 1, "Recoveries agree with recovery transactions", "accuracy", ("CDE-TX-001",),
     ("rrh.call_report_extract.amount", "edw.fact_loan_txn_daily.amount"),
     "Recoveries reported equal the recovery transactions in the quarter.",
     "For the quarter, RI-B total recoveries (RIAD4605) equals the sum of recovery transactions in "
     "the EDW within USD 1,000.", _usd(1000, 10000), ("CR-RIB-9-RE", "Y9C-HIB-9-RE"),
     ("NPB-DQ-TX-018",), {}),
    ("RP", 5, 1, "RC-N past-due amounts agree with arrears", "consistency", ("CDE-AR-002",),
     ("rrh.call_report_extract.amount", "edw.fact_arrears_daily.total_overdue"),
     "Past-due amounts reported come from arrears aging buckets.",
     "At each quarter-end, RC-N 30-89, 90+ and nonaccrual totals equal the EDW arrears totals by "
     "bucket within USD 1,000.", _usd(1000, 25000), ("CR-RCN-9-30", "CR-RCN-9-90", "CR-RCN-9-NA"),
     ("NPB-DQ-AR-003",), {}),
    ("RP", 6, 1, "BHCK2122 ties to the EDW loan total", "accuracy", ("CDE-LN-001",),
     ("rrh.fry9c_extract.amount", "edw.fact_loan_daily.principal_outstanding"),
     "Total loans on the FR Y-9C equal the warehouse total of loans net of unearned income.",
     "At each quarter-end, HC-C item 12 (BHCK2122) equals the EDW total of loans net of unearned "
     "income within USD 1,000.", _usd(1000, 25000), ("Y9C-HCC-12",), ("NPB-DQ-RP-001",),
     {"feed": "F-06"}),
    ("RP", 7, 1, "Allowance ties to the provision ledger", "accuracy", ("CDE-PV-001",),
     ("rrh.call_report_extract.amount", "edw.fact_loan_provision.provision_amount"),
     "The allowance reported equals the provision on the books.",
     "At each quarter-end, RC item 4.c (RCON3123) equals the EDW provision total within USD "
     "1,000.", _usd(1000, 10000), ("CR-RC-4C", "Y9C-HC-4C"), ("NPB-DQ-GL-008",), {}),
    ("RP", 8, 1, "Every RC-C category has a mapped product", "completeness", ("CDE-LN-001",),
     ("edw.fact_loan_daily.call_report_category",),
     "Each category the bank reports is fed by at least one product.",
     "For every RC-C category with a prior-quarter balance, at least one loan product maps to it.",
     _count(0, 0), ("CR-RCC-1C1", "CR-RCC-1C2A", "CR-RCC-1C2B", "CR-RCC-6C", "CR-RCC-6D"),
     ("NPB-DQ-LN-018",), {"population": ("RC-C categories", "RC-C category")}),
    ("RP", 9, 1, "Interest and fee income agrees with the portions", "accuracy",
     ("CDE-TX-003", "CDE-TX-004"),
     ("rrh.call_report_extract.amount", "edw.fact_loan_txn_daily.interest_portion",
      "edw.fact_loan_txn_daily.fee_portion"),
     "Interest and fee income reported equals the interest and fee portions collected.",
     "For the quarter, RI item 1.a (RIAD4010) equals the sum of interest and fee portions in the "
     "EDW, adjusted for accruals, within USD 5,000.", _usd(5000, 50000), ("CR-RI-1A",),
     ("NPB-DQ-TX-006",), {}),
    ("RP", 10, 1, "The extract is dated the quarter-end COB", "validity", ("CDE-LN-001",),
     ("rrh.call_report_extract.report_date",),
     "A quarterly report is built from the quarter-end close of business.",
     "For every extract, report_date is the last business day of the quarter in the NorthPeak "
     "business calendar.", _count(0, 0), ("CR-RCC-12", "Y9C-HCC-12"), (),
     {"population": ("quarterly extracts", "quarterly extract")}),
    # ---- FD: feeds (4) ----
    ("FD", 1, 1, "F-01 lands by 06:00 on each business day", "timeliness", ("CDE-LN-001",),
     ("edw.feed_audit.landed_at",),
     "The daily loan snapshot is in the warehouse before the business day starts.",
     "On every business day, F-01 (LSP to EDW loans) lands by 06:00 ET.", _mins(0, 60),
     (), ("NPB-DQ-FD-002", "NPB-DQ-LN-014"), {"feed": "F-01", "system": "S-EDW"}),
    ("FD", 2, 1, "F-01 row count matches the source", "completeness", ("CDE-LN-001",),
     ("edw.feed_audit.source_rows", "edw.feed_audit.target_rows"),
     "Every loan the platform holds arrives in the warehouse.",
     "On every business day, F-01 rows landed match the LSP row count within 0.1%.",
     _rel(0.001, 0.005), ("CR-RCC-12",), ("NPB-DQ-FD-001", "NPB-DQ-RP-001"),
     {"feed": "F-01", "system": "S-EDW"}),
    ("FD", 3, 1, "F-02 row count matches the source", "completeness", ("CDE-TX-001",),
     ("edw.feed_audit.source_rows", "edw.feed_audit.target_rows"),
     "Every transaction of the day arrives in the warehouse.",
     "On every business day, F-02 rows landed match the LSP transaction count within 0.1%.",
     _rel(0.001, 0.005), (), ("NPB-DQ-TX-016",), {"feed": "F-02", "system": "S-EDW"}),
    ("FD", 4, 1, "F-07 lands by 05:30 on each business day", "timeliness", ("CDE-AR-001",),
     ("edw.feed_audit.landed_at",),
     "Arrears are in the warehouse before the EDW snapshot.",
     "On every business day, F-07 (Collections to EDW arrears) lands by 05:30 ET.", _mins(0, 45),
     (), ("NPB-DQ-AR-001", "NPB-DQ-AR-010"), {"feed": "F-07", "system": "S-EDW"}),
)

#: Retired in v3.2 (Appendix C). An extractor must NOT propose these as live rules.
RETIRED_RULES = (
    ("NPB-DQ-LN-019", "Loan officer is assigned to every active loan",
     "Retired in v3.2: the loan officer is not a critical data element."),
    ("NPB-DQ-TX-019", "Every transaction carries a free-text note",
     "Retired in v3.2: notes are optional in the payments channels."),
    ("NPB-DQ-CL-011", "Client mobile number is in E.164 format",
     "Retired in v3.2: moved to the Customer Master's own controls."),
    ("NPB-DQ-GL-009", "Journal entries carry a description",
     "Retired in v3.2: descriptions are generated, not captured."),
)
VERSION_HISTORY = (
    ("3.0", "2025-07-01", "First rulebook under the CDE governance model (EF-2024-11 remediation)."),
    ("3.1", "2026-01-15", "Added the FD feed family and RP-006 (FR Y-9C HC-C tie-out)."),
    ("3.2", "2026-07-01", "Retired 4 rules (Appendix C) and re-graded 6: LN-003, TX-009, AR-001, "
                          "CL-004, GL-002 and RP-001. Each rule now carries its own Warning and "
                          "Error conditions; only Error opens or attaches an incident."),
)

_SEVERITY_DEFAULT = {"accuracy": "High", "consistency": "Medium", "validity": "Medium",
                     "completeness": "Medium", "timeliness": "High", "uniqueness": "High"}
_PCT = "{:.4f}"


def _fmt_pct(v: float) -> str:
    return _PCT.format(v * 100).rstrip("0").rstrip(".")


def _fmt_num(v: float) -> str:
    return f"{int(v):,}" if float(v).is_integer() else str(v)


def threshold_text(metric: str, value: float, population: tuple[str, str], also: str = "") -> str:
    """The rulebook sentence for one threshold (without the 'Warning when:' label)."""
    plural, singular = population
    if metric == "rate":
        s = (f"any {singular} fails at COB" if value == 0
             else f"more than {_fmt_pct(value)}% of {plural} fail at COB")
    elif metric == "count":
        s = (f"any {singular} fails at COB" if value == 0
             else f"more than one {singular} fails at COB" if value == 1
             else f"more than {_fmt_num(value)} {plural} fail at COB")
    elif metric == "relative_diff":
        s = f"the two totals differ by more than {_fmt_pct(value)}%"
    elif metric == "amount_usd":
        s = ("the two totals differ at all" if value == 0
             else f"the two totals differ by more than USD {_fmt_num(value)}")
    elif metric == "minutes":
        s = ("the deadline is missed" if value == 0
             else f"the deadline is missed by more than {_fmt_num(value)} minutes")
    else:
        raise ValueError(metric)
    if also:
        s += f", or {also}"
    return s + "."


def _build_rules() -> tuple[DqRule, ...]:
    out = []
    for (fam, n, ver, title, dim, cdes, cols, statement, check, thr, regs, related,
         extra) in _RULE_ROWS:
        fname, ptable, owner, feed, pop, base = FAMILIES[fam]
        pop = extra.get("population", pop)
        rid = f"NPB-DQ-{fam}-{n:03d}"
        metric, w, e, also = thr
        tables = tuple(dict.fromkeys(c.rsplit(".", 1)[0] for c in cols))
        system = extra.get("system") or ("S-RRH" if fam == "RP" else "S-LSP")
        feed_id = extra.get("feed", feed)
        warn_text = ("never: any failure is graded Error." if w == e
                     else threshold_text(metric, w, pop))
        warn = DqThreshold(f"{rid}:warning", rid, "Warning", metric, w, "", warn_text)
        err = DqThreshold(f"{rid}:error", rid, "Error", metric, e, also,
                          threshold_text(metric, e, pop, also))
        sev = "High" if regs and dim in ("accuracy", "consistency", "timeliness") else \
            _SEVERITY_DEFAULT[dim] if regs else ("Low" if dim == "completeness" else "Medium")
        owner_team = owner
        handling = (f"Warning: DQ dashboard only. Error: opens an incident for "
                    f"{_owner_name(owner_team)} (Service Desk {_owner_group(owner_team)}) or attaches to "
                    f"the open one for this rule, with the failing keys.")
        out.append(DqRule(rid, ver, f"DQX-{base + n:04d}", fam, title, statement, dim, cdes, cols,
                          tables, check, pop[0], warn, err, sev, owner_team, system, feed_id, regs,
                          handling, related, extra.get("legal")))
    return tuple(out)


def _owner_name(owner_id: str) -> str:
    return next(o.name for o in OWNERS if o.owner_id == owner_id)


def _owner_group(owner_id: str) -> str:
    return next(o.service_desk_group for o in OWNERS if o.owner_id == owner_id)


# ---------------------------------------------------------------------------------------------
# Business calendar (US Federal Reserve holidays, 2026)
# ---------------------------------------------------------------------------------------------
CALENDAR_ID = "NPB-US-2026"
CALENDAR_NAME = "NorthPeak Bank US business calendar 2026"
HOLIDAYS_2026 = {
    "2026-01-01": "New Year's Day", "2026-01-19": "Birthday of Martin Luther King, Jr.",
    "2026-02-16": "Washington's Birthday", "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day (observed)", "2026-09-07": "Labor Day",
    "2026-10-12": "Columbus Day", "2026-11-11": "Veterans Day",
    "2026-11-26": "Thanksgiving Day", "2026-12-25": "Christmas Day",
}
CALENDAR_START, CALENDAR_END = date(2026, 9, 1), date(2026, 12, 31)


def _calendar() -> tuple[CalendarDay, ...]:
    days, d = [], CALENDAR_START
    quarter_ends = set()
    while d <= CALENDAR_END:
        days.append(d)
        d += timedelta(days=1)
    business = [x for x in days if x.weekday() < 5 and x.isoformat() not in HOLIDAYS_2026]
    for month in (9, 12):
        quarter_ends.add(max(x for x in business if x.month == month))
    return tuple(CalendarDay(x.isoformat(), x.strftime("%A"), x in business,
                             HOLIDAYS_2026.get(x.isoformat()), x in quarter_ends) for x in days)


# ---------------------------------------------------------------------------------------------
# Context Graph seed: resolved patterns, closed incidents, precedents, one open incident
# ---------------------------------------------------------------------------------------------
PATTERNS: tuple[ResolvedPattern, ...] = (
    ResolvedPattern("RPAT-COB-ORDER", "Derived balances stale at extract (COB job ordering)",
                    "source_processing",
                    "Backdated repayments or reversals are posted late in the day; the LSP's "
                    "'Update Loan Summary' COB job recomputes the *_derived balances, but the F-01 "
                    "extract starts before it finishes, so principal outstanding no longer equals "
                    "its components for the affected loans.",
                    "LN-003 breaks clustered on loans with a backdated reversal on the COB date; "
                    "breaks clear on the next COB without intervention.",
                    ("NPB-DQ-LN-003", "NPB-DQ-LN-004", "NPB-DQ-LN-014", "NPB-DQ-TX-004"),
                    "Make F-01 wait for the LSP COB completion flag, under change control."),
    ResolvedPattern("RPAT-FEED-LATE", "Feed landed after the downstream snapshot", "feed_etl",
                    "A daily feed lands after its SLA; the downstream snapshot reads the previous "
                    "COB's data.", "Failures on one feed's target tables only, all on one COB.",
                    ("NPB-DQ-FD-001", "NPB-DQ-FD-004", "NPB-DQ-AR-001"),
                    "Gate the downstream job on the feed's landing, alert on SLA breach."),
    ResolvedPattern("RPAT-SHORTPAY", "Short payment marked an installment completed",
                    "source_processing",
                    "A repayment smaller than the installment due was allocated straight to the "
                    "installment and flagged it completed instead of being held in suspense "
                    "(Regulation Z partial-payment handling).",
                    "SC-004 and TX-010 fail together on loans with a payment below the installment "
                    "amount.", ("NPB-DQ-SC-004", "NPB-DQ-TX-010", "NPB-DQ-TX-009"),
                    "Repayment allocation holds short payments in suspense per product."),
    ResolvedPattern("RPAT-ONBOARD-FIELD", "Optional field at onboarding left a CDE blank",
                    "manual_entry", "A channel made a CDE optional; staff skipped it.",
                    "Completeness failures concentrated on one onboarding channel.",
                    ("NPB-DQ-CL-002", "NPB-DQ-CL-003"), "Make the field mandatory in the channel."),
    ResolvedPattern("RPAT-GL-MAP", "Product mapped to the wrong GL account", "reference_data",
                    "A new or changed loan product was mapped to the wrong GL account in "
                    "acc_product_mapping, so its balances post outside loans receivable.",
                    "GL-002 variance equals the balance of one product.",
                    ("NPB-DQ-GL-002", "NPB-DQ-GL-007"),
                    "Four-eyes review of acc_product_mapping changes."),
    ResolvedPattern("RPAT-DUP-CIF", "Customer re-onboarded creates a duplicate id",
                    "manual_entry", "Branch staff re-onboard an existing customer instead of "
                    "finding them, creating a second Customer Master record and external id.",
                    "CL-001 duplicates where names and dates of birth match.",
                    ("NPB-DQ-CL-001", "NPB-DQ-CL-009"),
                    "Duplicate check at onboarding plus a weekly merge."),
    ResolvedPattern("RPAT-THRESHOLD-UNIT", "DQ Monitor threshold entered in the wrong unit",
                    "rule_config", "A threshold was entered as a percent where the DQ Monitor expects a "
                    "fraction (or the reverse), so the DQ Monitor grades Error on a failure rate the "
                    "rulebook treats as a Warning or clean.",
                    "The DQ Monitor's configured thresholds differ from the rulebook's by a factor of 100.",
                    ("NPB-DQ-CL-004",),
                    "Validate threshold units on rule-pack import against the rulebook."),
    ResolvedPattern("RPAT-PARTIAL-LOAD", "Report extract built on a partial feed load",
                    "feed_etl", "A quarterly extract ran while a daily feed had only partly "
                    "landed.", "Report tie-out breaks equal to the missing rows' balance.",
                    ("NPB-DQ-RP-001", "NPB-DQ-FD-002"),
                    "Gate the extract on the feed's row-count check."),
)

INCIDENTS: tuple[Incident, ...] = (
    Incident("INC-1988", "F-01 landed three hours late after warehouse maintenance overran",
             "2026-03-16", "2026-03-18", "closed", "NPB-DQ-FD-001", ("CDE-LN-001",), "High",
             "OWN-DATAPLAT", "feed_etl", "RPAT-FEED-LATE",
             "EDW maintenance window overran into the F-01 load slot.",
             ("LSP COB job failure", "network outage"),
             "Moved the maintenance window; added an SLA-breach alert on F-01.", "process_change",
             "held", "No recurrence of the maintenance cause in 200 days."),
    Incident("INC-2041", "Principal outstanding breaks after backdated reversals (overposted "
             "principal plus interest)", "2026-04-20", "2026-04-24", "closed", "NPB-DQ-LN-003",
             ("CDE-LN-001",), "High", "OWN-LOANOPS", "source_processing", "RPAT-COB-ORDER",
             "Backdated repayment reversals left principal_outstanding_derived stale: the "
             "repayment had overposted principal plus interest and the reversal was not reflected "
             "before the F-01 extract.", ("F-01 partial load", "GL mapping change"),
             "Data patch: recomputed derived balances for 318 loans.", "data_patch",
             "did_not_hold", "Recurred on 2026-07-06 (INC-2187): the patch fixed data, not the "
             "job ordering."),
    Incident("INC-2059", "Short payments marked installments completed", "2026-05-04",
             "2026-05-15", "closed", "NPB-DQ-SC-004", ("CDE-SC-002", "CDE-TX-001"), "High",
             "OWN-LOANOPS", "source_processing", "RPAT-SHORTPAY",
             "Repayment allocation for two products credited payments below the installment "
             "amount directly and set completed_derived, instead of holding them in suspense.",
             ("manual adjustment", "schedule regeneration"),
             "Code fix to the allocation strategy; affected installments re-opened.", "code_fix",
             "held", "No recurrence on the fixed products in 170 days."),
    Incident("INC-2087", "Date of birth missing for clients onboarded on branch tablets",
             "2026-05-21", "2026-05-29", "closed", "NPB-DQ-CL-002", ("CDE-CL-002",), "Medium",
             "OWN-CUSTDATA", "manual_entry", "RPAT-ONBOARD-FIELD",
             "The branch tablet app made date of birth optional.", ("CIF sync failure",),
             "Made the field mandatory; back-filled 112 records.", "process_change", "held",
             "Held since release."),
    Incident("INC-2112", "GL reconciliation break after a new product mapping", "2026-06-08",
             "2026-06-10", "closed", "NPB-DQ-GL-002", ("CDE-GL-001", "CDE-LN-001"), "High",
             "OWN-FIN", "reference_data", "RPAT-GL-MAP",
             "Product 'Auto Prime 2026' was mapped to the deposits suspense account instead of "
             "loans receivable.", ("F-03 journal delay", "LSP balance error"),
             "Corrected acc_product_mapping; introduced four-eyes review.", "config_change",
             "held", "Held for products mapped under four-eyes review."),
    Incident("INC-2150", "Duplicate client external ids from re-onboarding", "2026-06-29",
             "2026-07-10", "closed", "NPB-DQ-CL-001", ("CDE-CL-001",), "Medium", "OWN-CUSTDATA",
             "manual_entry", "RPAT-DUP-CIF",
             "Branch staff re-onboarded 23 existing customers, creating duplicate CIF records.",
             ("CIF sync replay",), "Duplicate check at onboarding; merged the duplicates.",
             "process_change", "partial", "Three low-volume recurrences since; the check is "
             "bypassed when the branch app is offline."),
    Incident("INC-2187", "Principal outstanding breaks again: F-01 started before the COB "
             "summary job", "2026-07-06", "2026-07-09", "closed", "NPB-DQ-LN-003",
             ("CDE-LN-001",), "High", "OWN-LSPENG", "source_processing", "RPAT-COB-ORDER",
             "The F-01 extract started before the LSP 'Update Loan Summary' COB job had "
             "recomputed balances for loans with backdated reversals.",
             ("F-01 partial load", "reversal posting defect"),
             "F-01 now waits for the LSP COB completion flag (job dependency).", "config_change",
             "held_until_regressed", "Held 88 days, until LSP release 1.11.3 on 2026-10-02 "
             "(CHG-7731) removed the dependency.", correlated_with=("INC-2041",)),
    Incident("INC-2203", "Age-range rule flagged Error on a normal week (false positive)",
             "2026-07-20", "2026-07-22", "closed", "NPB-DQ-CL-004", ("CDE-CL-002",), "Medium",
             "OWN-DQOPS", "rule_config", "RPAT-THRESHOLD-UNIT",
             "The DQ Monitor had the CL-004 thresholds entered as percentages where it expects fractions; "
             "the data matched its baseline.", ("onboarding defect", "CIF sync"),
             "Corrected the DQ Monitor's thresholds; Rule Proposal case RD-014 asked for unit validation.",
             "config_change", "held_until_regressed",
             "Held until the rule-pack re-import of 2026-10-05 (CHG-7745)."),
    Incident("INC-2219", "Q2 Call Report RCON2122 off by USD 3.1M", "2026-07-08", "2026-07-11",
             "closed", "NPB-DQ-RP-001", ("CDE-LN-001",), "High", "OWN-REGREP", "feed_etl",
             "RPAT-PARTIAL-LOAD", "F-05 ran on a partial F-01 load after a restart.",
             ("RC-C mapping error",), "F-05 gated on the FD-002 row-count check.", "process_change",
             "held", "Held for Q3."),
    Incident("INC-2240", "Overdue-since set late: F-07 landed after the EDW snapshot",
             "2026-08-12", "2026-08-14", "closed", "NPB-DQ-AR-001", ("CDE-AR-001",), "High",
             "OWN-COLL", "feed_etl", "RPAT-FEED-LATE",
             "The Collections batch overran and F-07 landed after the EDW arrears snapshot.",
             ("arrears aging job defect",), "Re-ran F-07 and the snapshot; added a landing gate.",
             "process_change", "partial", "The gate was not applied on Mondays (ISS-052)."),
    # The one open incident (opened by Sentinel from EXG-20261002-0002).
    Incident("INC-2301", "Overdue-since set late on COB 2026-10-02", "2026-10-02", None,
             "investigating", "NPB-DQ-AR-001", ("CDE-AR-001",), "High", "OWN-COLL", None, None,
             None, (), None, None, None, None, raised_from="EXG-20261002-0002"),
)

PRECEDENTS: tuple[Precedent, ...] = tuple(
    Precedent(f"PRE-{i.incident_id[4:]}", i.incident_id, i.pattern_id,
              f"{i.title}. Cause: {i.confirmed_cause} Fix: {i.fix} Durability: "
              f"{i.fix_durability} ({i.durability_note})")
    for i in INCIDENTS if i.status == "closed")

ISSUES: tuple[Issue, ...] = (
    Issue("ISS-031", "LSP COB job dependencies are not under change control", "open",
          "2026-07-10", "OWN-LSPENG",
          (("system", "S-LSP"), ("data_quality_rule", "NPB-DQ-LN-003"), ("feed", "F-01")),
          description="Raised after INC-2187: the F-01 dependency on the COB completion flag can "
                      "be removed by a platform release without review."),
    Issue("ISS-044", "FR Y-9C extract lineage is not registered in the lineage catalogue", "open",
          "2026-03-02", "OWN-REGREP",
          (("feed", "F-06"), ("database_table", "rrh.fry9c_extract"),
           ("data_quality_rule", "NPB-DQ-RP-006")), ("EF-2025-02",),
          "The EDW to RRH hop for FR Y-9C (F-06) is undocumented column by column."),
    Issue("ISS-047", "DQ Monitor rule-pack import does not validate threshold units", "open",
          "2026-07-22", "OWN-DQOPS",
          (("system", "S-DQM"), ("data_quality_rule", "NPB-DQ-CL-004")),
          description="Rule Proposal case RD-014 from INC-2203."),
    Issue("ISS-052", "F-07 arrears feed lands after the EDW snapshot on Mondays", "open",
          "2026-08-20", "OWN-COLL",
          (("feed", "F-07"), ("data_quality_rule", "NPB-DQ-AR-001")),
          description="The INC-2240 landing gate is skipped by the Monday batch schedule."),
    Issue("ISS-058", "RC-N past-due buckets computed from the EDW snapshot, not arrears aging",
          "closed", "2025-08-01", "OWN-REGREP",
          (("data_quality_rule", "NPB-DQ-RP-005"), ("database_table", "edw.fact_arrears_daily")),
          ("EF-2025-07",), "Re-pointed the RC-N build to arrears aging buckets."),
)

FINDINGS: tuple[ExaminationFinding, ...] = (
    ExaminationFinding("EF-2024-11", "DQ thresholds for CDEs were not approved by CDE stewards",
                       "MRA", "Federal Reserve Bank", "2024-11-14", "2025-06-30", "closed",
                       (("cde", "CDE-LN-001"), ("cde", "CDE-AR-001")),
                       "Remediated by rulebook v3.0: every threshold is approved by the CDE's "
                       "steward."),
    ExaminationFinding("EF-2025-02", "End-to-end lineage for FR Y-9C HC-C is not documented",
                       "MRA", "Federal Reserve Bank", "2025-02-20", "2026-12-31", "open",
                       (("report_line", "Y9C-HCC-12"), ("cde", "CDE-LN-001"), ("feed", "F-06"),
                        ("data_quality_rule", "NPB-DQ-RP-006")),
                       "The bank cannot show column-level lineage from the loan platform to "
                       "BHCK2122."),
    ExaminationFinding("EF-2025-07", "RC-N past-due misclassification for loans with short "
                       "payments", "Finding", "FDIC", "2025-07-09", "2026-03-31", "open",
                       (("report_line", "CR-RCN-9-30"), ("data_quality_rule", "NPB-DQ-AR-003"),
                        ("data_quality_rule", "NPB-DQ-TX-009")),
                       "Short payments credited to installments understated 30-89 day past "
                       "dues."),
    ExaminationFinding("EF-2026-01", "Recurring loan-balance breaks lack a durable root-cause fix",
                       "Observation", "Federal Reserve Bank", "2026-08-03", "2026-12-31", "open",
                       (("data_quality_rule", "NPB-DQ-LN-003"), ("cde", "CDE-LN-001")),
                       "INC-2041 and INC-2187 share a cause; the first fix was a data patch."),
    ExaminationFinding("EF-2026-03", "Feed SLAs are not aligned to the business calendar",
                       "Observation", "FDIC", "2026-09-10", "2027-03-31", "open",
                       (("business_calendar", CALENDAR_ID), ("data_quality_rule", "NPB-DQ-FD-001"),
                        ("feed", "F-07")),
                       "Feed SLAs ignore holidays and the Monday batch schedule."),
)

# ---------------------------------------------------------------------------------------------
# Profile Service: recent changes per platform
# ---------------------------------------------------------------------------------------------
CHANGES: tuple[Change, ...] = (
    Change("CHG-7690", "2026-09-15", "S-LSP", "Repayment allocation strategy changed for product "
           "'Home Equity Flex' (new-style allocation).", ("NPB-DQ-SC-004", "NPB-DQ-TX-010")),
    Change("CHG-7702", "2026-09-24", "S-LSP", "New loan product 'Green Auto 2026' mapped in "
           "acc_product_mapping.", ("NPB-DQ-GL-002", "NPB-DQ-GL-007")),
    Change("CHG-7712", "2026-09-28", "S-CIF", "Branch onboarding app 4.2 released (offline "
           "mode).", ("NPB-DQ-CL-001",), ("F-04",)),
    Change("CHG-7720", "2026-09-30", "S-RRH", "FR Y-9C Q3 2026 template update loaded.",
           ("NPB-DQ-RP-006",), ("F-06",)),
    Change("CHG-7738", "2026-10-01", "S-COL", "Collections batch window moved 30 minutes later.",
           ("NPB-DQ-AR-001", "NPB-DQ-FD-004"), ("F-07",)),
    Change("CHG-7731", "2026-10-02", "S-LSP", "LSP release 1.11.3: COB job chain rebuilt; the F-01 "
           "trigger no longer waits for 'Update Loan Summary'.",
           ("NPB-DQ-LN-003", "NPB-DQ-LN-014"), ("F-01",)),
    Change("CHG-7745", "2026-10-05", "S-DQM", "DQ Monitor rule pack re-imported (CL family v3).",
           ("NPB-DQ-CL-004",)),
    Change("CHG-7750", "2026-10-05", "S-EDW", "EDW cluster patch window (22:00-05:30 ET).",
           ("NPB-DQ-FD-001",), ("F-01",)),
)

# ---------------------------------------------------------------------------------------------
# The DQ Monitor exception groups and the answer key's planted outcomes
# ---------------------------------------------------------------------------------------------
# (group id, rule id, cob, column, population, failed, measured or None (=failed/population),
#  monitor thresholds override or None, emitted_at, note)
_GROUP_ROWS = (
    ("EXG-20261001-0001", "NPB-DQ-TX-006", "2026-10-01", "m_loan_transaction.amount", 18450, 4,
     None, None, "2026-10-02T05:12:00-04:00", ""),
    ("EXG-20261001-0003", "NPB-DQ-GL-002", "2026-10-01", "gl.ledger_balance.balance", 14, 1,
     0.00083, None, "2026-10-02T06:41:00-04:00",
     "Variance 0.083% on product 'Green Auto 2026'."),
    ("EXG-20261002-0002", "NPB-DQ-AR-001", "2026-10-02",
     "m_loan_arrears_aging.overdue_since_date_derived", 3912, 61, None, None,
     "2026-10-05T05:58:00-04:00", "Opened INC-2301."),
    ("EXG-20261002-0004", "NPB-DQ-CH-001", "2026-10-02", "m_loan_charge.amount_outstanding_derived",
     22310, 5, None, None, "2026-10-05T06:02:00-04:00", ""),
    ("EXG-20261005-0003", "NPB-DQ-LN-003", "2026-10-05", "m_loan.principal_outstanding_derived",
     61220, 412, None, None, "2026-10-06T06:17:00-04:00",
     "Running example: DQX-0588 v2 on COB 2026-10-05."),
    ("EXG-20261005-0005", "NPB-DQ-AR-001", "2026-10-05",
     "m_loan_arrears_aging.overdue_since_date_derived", 3950, 48, None, None,
     "2026-10-06T06:20:00-04:00", ""),
    ("EXG-20261006-0001", "NPB-DQ-FD-001", "2026-10-06", "edw.feed_audit.landed_at", 1, 1, 95,
     None, "2026-10-06T07:35:00-04:00", "F-01 landed at 07:35 ET."),
    ("EXG-20261006-0002", "NPB-DQ-CL-004", "2026-10-06", "m_client.date_of_birth", 48700, 19,
     None, {"warning": 0.000002, "error": 0.00005}, "2026-10-07T05:44:00-04:00", ""),
    ("EXG-20261006-0005", "NPB-DQ-SC-004", "2026-10-06",
     "m_loan_repayment_schedule.completed_derived", 410880, 230, None, None,
     "2026-10-07T05:51:00-04:00", ""),
    ("EXG-20261006-0007", "NPB-DQ-LN-003", "2026-10-06", "m_loan.principal_outstanding_derived",
     61240, 398, None, None, "2026-10-07T06:09:00-04:00", ""),
    ("EXG-20261007-0001", "NPB-DQ-CL-001", "2026-10-07", "m_client.external_id", 48712, 14, None,
     None, "2026-10-08T05:40:00-04:00", "14 duplicate external ids."),
    ("EXG-20261007-0004", "NPB-DQ-RP-006", "2026-10-07", "rrh.fry9c_extract.amount", 1, 1,
     2480000, None, "2026-10-08T09:15:00-04:00",
     "Q3 2026 FR Y-9C extract (report date 2026-09-30): BHCK2122 differs from the EDW total by "
     "USD 2,480,000."),
    ("EXG-20261007-0006", "NPB-DQ-TX-009", "2026-10-07", "m_loan_transaction.amount", 19020, 2,
     None, None, "2026-10-08T05:47:00-04:00", ""),
)

#: The expected pipeline outcome per exception group. Never shown to an agent.
_EXPECTED = {
    "EXG-20261001-0001": dict(incident_action="ignore", attach_to=None, root_cause_family=None,
                              precedent=None, pattern=None, lane=None, loop_back=False),
    "EXG-20261001-0003": dict(incident_action="open", attach_to=None,
                              root_cause_family="reference_data", precedent="INC-2112",
                              pattern="RPAT-GL-MAP", lane="lane_reference_data", loop_back=False,
                              recent_change="CHG-7702"),
    "EXG-20261002-0002": dict(incident_action="open", attach_to=None, opened="INC-2301",
                              root_cause_family="feed_etl", precedent="INC-2240",
                              pattern="RPAT-FEED-LATE", lane="lane_feed_etl", loop_back=False,
                              recent_change="CHG-7738"),
    "EXG-20261002-0004": dict(incident_action="ignore", attach_to=None, root_cause_family=None,
                              precedent=None, pattern=None, lane=None, loop_back=False),
    "EXG-20261005-0003": dict(incident_action="open", attach_to=None,
                              root_cause_family="source_processing", precedent="INC-2187",
                              also_precedents=("INC-2041",), pattern="RPAT-COB-ORDER",
                              lane="lane_source_processing", loop_back=False,
                              recent_change="CHG-7731", open_issue="ISS-031",
                              finding="EF-2026-01"),
    "EXG-20261005-0005": dict(incident_action="attach", attach_to="INC-2301",
                              root_cause_family="feed_etl", precedent="INC-2240",
                              pattern="RPAT-FEED-LATE", lane="lane_feed_etl", loop_back=False),
    "EXG-20261006-0001": dict(incident_action="open", attach_to=None, root_cause_family="feed_etl",
                              precedent="INC-1988", pattern="RPAT-FEED-LATE", lane="lane_feed_etl",
                              loop_back=False, recent_change="CHG-7750"),
    "EXG-20261006-0002": dict(incident_action="open", attach_to=None,
                              root_cause_family="rule_config", precedent="INC-2203",
                              pattern="RPAT-THRESHOLD-UNIT", lane="rule_drafting",
                              loop_back=False, false_positive=True, recent_change="CHG-7745",
                              open_issue="ISS-047"),
    "EXG-20261006-0005": dict(incident_action="open", attach_to=None,
                              root_cause_family="source_processing", precedent="INC-2059",
                              pattern="RPAT-SHORTPAY", lane="lane_source_processing",
                              loop_back=False, recent_change="CHG-7690"),
    "EXG-20261006-0007": dict(incident_action="attach",
                              attach_to="incident opened for EXG-20261005-0003",
                              root_cause_family="source_processing", precedent="INC-2187",
                              pattern="RPAT-COB-ORDER", lane="lane_source_processing",
                              loop_back=False),
    "EXG-20261007-0001": dict(incident_action="open", attach_to=None,
                              root_cause_family="manual_entry", precedent="INC-2150",
                              pattern="RPAT-DUP-CIF", lane="lane_manual_entry", loop_back=False,
                              recent_change="CHG-7712"),
    "EXG-20261007-0004": dict(incident_action="open", attach_to=None, root_cause_family=None,
                              precedent=None, pattern=None, lane="insufficient_evidence",
                              loop_back=True, coverage_gap="lineage edw.fact_loan_daily -> "
                              "rrh.fry9c_extract (F-06) not registered",
                              open_issue="ISS-044", finding="EF-2025-02"),
    "EXG-20261007-0006": dict(incident_action="ignore", attach_to=None, root_cause_family=None,
                              precedent=None, pattern=None, lane=None, loop_back=False),
}


# ---------------------------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------------------------
def grade(rule: DqRule, measured: float, thresholds: dict[str, float] | None = None) -> str:
    """Grade a measured value against a rule's thresholds: "Error", "Warning" or "Clean".

    `measured` is in the rule's metric (rate = failed/population, count, minutes late,
    relative difference, USD difference). `thresholds` overrides {"warning", "error"} (the DQ Monitor's
    configured values, which may differ from the rulebook's)."""
    w = thresholds["warning"] if thresholds else rule.warning.value
    e = thresholds["error"] if thresholds else rule.error.value
    if measured > e:
        return "Error"
    if measured > w:
        return "Warning"
    return "Clean"


# ---------------------------------------------------------------------------------------------
# The dataset
# ---------------------------------------------------------------------------------------------
@dataclass
class Dataset:
    seed: int
    owners: tuple[Owner, ...]
    systems: tuple[System, ...]
    structures: tuple[DatabaseStructure, ...]
    tables: tuple[DatabaseTable, ...]
    feeds: tuple[Feed, ...]
    lineage: tuple[Lineage, ...]
    cdes: tuple[Cde, ...]
    rules: tuple[DqRule, ...]
    retired_rules: tuple[tuple[str, str, str], ...]
    reports: tuple[RegulatoryReport, ...]
    report_lines: tuple[ReportLine, ...]
    mdrms: tuple[Mdrm, ...]
    calendar: tuple[CalendarDay, ...]
    incidents: tuple[Incident, ...]
    patterns: tuple[ResolvedPattern, ...]
    precedents: tuple[Precedent, ...]
    issues: tuple[Issue, ...]
    findings: tuple[ExaminationFinding, ...]
    exception_groups: tuple[ExceptionGroup, ...]
    profiles: tuple[PlatformProfile, ...]
    changes: tuple[Change, ...]
    _index: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # -- lookups ------------------------------------------------------------------------------
    def rule(self, rule_id: str) -> DqRule:
        return self._idx("rule", self.rules, "rule_id")[rule_id]

    def rule_by_monitor_id(self, monitor_id: str) -> DqRule:
        return self._idx("dq-monitor", self.rules, "monitor_id")[monitor_id]

    def cde(self, cde_id: str) -> Cde:
        return self._idx("cde", self.cdes, "cde_id")[cde_id]

    def table(self, key: str) -> DatabaseTable:
        return self._idx("table", self.tables, "key")[key]

    def column(self, key: str) -> DatabaseColumn:
        if "column" not in self._index:
            self._index["column"] = {c.key: c for t in self.tables for c in t.columns}
        return self._index["column"][key]

    def system(self, system_id: str) -> System:
        return self._idx("system", self.systems, "system_id")[system_id]

    def feed(self, feed_id: str) -> Feed:
        return self._idx("feed", self.feeds, "feed_id")[feed_id]

    def owner(self, owner_id: str) -> Owner:
        return self._idx("owner", self.owners, "owner_id")[owner_id]

    def report_line(self, line_id: str) -> ReportLine:
        return self._idx("line", self.report_lines, "line_id")[line_id]

    def incident(self, incident_id: str) -> Incident:
        return self._idx("incident", self.incidents, "incident_id")[incident_id]

    def exception_group(self, group_id: str) -> ExceptionGroup:
        return self._idx("group", self.exception_groups, "group_id")[group_id]

    def profile(self, platform: str) -> PlatformProfile:
        return self._idx("profile", self.profiles, "platform")[platform]

    def calendar_day(self, day: str) -> CalendarDay:
        return self._idx("day", self.calendar, "day")[day]

    def rules_in_family(self, family: str) -> tuple[DqRule, ...]:
        return tuple(r for r in self.rules if r.family == family)

    def open_incidents(self) -> tuple[Incident, ...]:
        return tuple(i for i in self.incidents if i.status != "closed")

    def lineage_upstream(self, key: str) -> list[Lineage]:
        """Every registered hop that ends (transitively) at a column or report line."""
        out, frontier, seen = [], [key], set()
        while frontier:
            k = frontier.pop()
            for hop in self.lineage:
                if hop.to == k and hop.lineage_id not in seen:
                    seen.add(hop.lineage_id)
                    out.append(hop)
                    frontier.append(hop.from_column)
        return out

    def _idx(self, name: str, items, attr: str) -> dict:
        if name not in self._index:
            self._index[name] = {getattr(x, attr): x for x in items}
        return self._index[name]

    # -- graph projection ---------------------------------------------------------------------
    def nodes(self) -> list[dict[str, Any]]:
        """Every Knowledge and Context Graph node: {kind, key, name, attrs}."""
        out: list[dict[str, Any]] = []

        def add(kind: str, key: str, name: str, obj: Any = None, **attrs: Any) -> None:
            data = {}
            if obj is not None:
                data = {k: v for k, v in asdict(obj).items() if not k.startswith("_")}
            data.update(attrs)
            out.append({"kind": kind, "key": key, "name": name, "attrs": data})

        for o in self.owners:
            add("owner", o.owner_id, o.name, o)
        for s in self.systems:
            add("system", s.system_id, s.name, s)
        for d in self.structures:
            add("database_structure", d.structure_id, d.name, d)
        for t in self.tables:
            add("database_table", t.key, t.name, None, structure_id=t.structure_id,
                system_id=t.system_id, description=t.description, fineract=t.fineract,
                in_code_graph=t.in_code_graph)
            for c in t.columns:
                add("database_column", c.key, c.key, c)
        for f in self.feeds:
            add("feed", f.feed_id, f.name, f)
        for ln in self.lineage:
            add("lineage", ln.lineage_id, f"{ln.from_column} -> {ln.to}", ln)
            if ln.transform not in PLAIN_TRANSFORMS:
                add("transformation", f"TRF-{ln.lineage_id}", ln.transform, None,
                    lineage_id=ln.lineage_id, description=ln.transform)
        for family, meaning in ROOT_CAUSE_FAMILIES.items():
            add("root_cause_category", family, ROOT_CAUSE_FAMILY_NAMES[family], None,
                family=family, description=meaning)
        for p in self.patterns:
            add("remediation_template", remediation_template_id(p.pattern_id), p.durable_fix,
                None, pattern_id=p.pattern_id, family=p.family, fix=p.durable_fix)
        for c in self.cdes:
            add("cde", c.cde_id, c.name, c)
        for r in self.rules:
            data = {k: v for k, v in asdict(r).items() if k not in ("warning", "error")}
            data["warning"], data["error"] = r.warning.text, r.error.text
            out.append({"kind": "data_quality_rule", "key": r.rule_id,
                        "name": f"{r.rule_id} v{r.version}: {r.title}", "attrs": data})
            for t in (r.warning, r.error):
                add("dq_threshold", t.threshold_id, f"{r.rule_id} {t.level}", t)
        for rep in self.reports:
            add("regulatory_report", rep.report_id, rep.name, rep)
        for line in self.report_lines:
            add("report_line", line.line_id, f"{line.schedule} item {line.item}: {line.caption}",
                line)
        for m in self.mdrms:
            add("mdrm", m.code, m.code, m, illustrative=True)
        add("business_calendar", CALENDAR_ID, CALENDAR_NAME, None,
            start=CALENDAR_START.isoformat(), end=CALENDAR_END.isoformat(),
            holidays=dict(HOLIDAYS_2026))
        for d in self.calendar:
            add("calendar_day", d.day, d.day, d)
        for i in self.issues:
            add("issue", i.issue_id, i.title, i)
        for f in self.findings:
            add("examination_finding", f.finding_id, f.title, f)
        for g in self.exception_groups:
            add("exception_group", g.group_id, f"{g.monitor_rule_id} v{g.rule_version} on COB "
                f"{g.cob}", g)
        for i in self.incidents:
            add("incident", i.incident_id, i.title, i)
        for p in self.precedents:
            add("precedent", p.precedent_id, p.summary[:120], p)
        for p in self.patterns:
            add("resolved_pattern", p.pattern_id, p.name, p)
        return out

    def edges(self) -> list[tuple[str, str, str, str, str]]:
        """Every relationship: (source kind, source key, relationship, target kind, target key)."""
        e: list[tuple[str, str, str, str, str]] = []
        add = e.append
        for o in self.owners:
            if o.team_id:
                add(("owner", o.owner_id, "MEMBER_OF", "owner", o.team_id))
        for s in self.systems:
            add(("owner", s.owner_id, "OWNS", "system", s.system_id))
            add(("system", s.system_id, "USES_CALENDAR", "business_calendar", CALENDAR_ID))
        for d in self.structures:
            add(("system", d.system_id, "HOSTS", "database_structure", d.structure_id))
        for t in self.tables:
            add(("database_structure", t.structure_id, "CONTAINS", "database_table", t.key))
            add(("system", t.system_id, "OWNS", "database_table", t.key))
            for c in t.columns:
                add(("database_table", t.key, "HAS_COLUMN", "database_column", c.key))
        for f in self.feeds:
            add(("owner", f.owner_id, "OWNS", "feed", f.feed_id))
            add(("feed", f.feed_id, "FROM_SYSTEM", "system", f.source_system))
            add(("feed", f.feed_id, "TO_SYSTEM", "system", f.target_system))
            for t in f.reads:
                add(("feed", f.feed_id, "READS", "database_table", t))
            for t in f.feeds:
                add(("feed", f.feed_id, "FEEDS", "database_table", t))
        for ln in self.lineage:
            add(("lineage", ln.lineage_id, "FROM", "database_column", ln.from_column))
            add(("lineage", ln.lineage_id, "TO", ln.to_kind, ln.to))
            if ln.feed_id:
                add(("lineage", ln.lineage_id, "VIA", "feed", ln.feed_id))
            if ln.transform not in PLAIN_TRANSFORMS:
                add(("lineage", ln.lineage_id, "APPLIES", "transformation", f"TRF-{ln.lineage_id}"))
        for c in self.cdes:
            add(("owner", c.steward_id, "STEWARDS", "cde", c.cde_id))
            add(("cde", c.cde_id, "SYSTEM_OF_RECORD", "system", c.system_of_record))
            for col in c.columns:
                add(("cde", c.cde_id, "STORED_IN", "database_column", col))
            for up in c.upstream:
                add(("cde", c.cde_id, "DERIVED_FROM", "cde", up))
            for line in c.reported_in:
                add(("cde", c.cde_id, "REPORTED_IN", "report_line", line))
        for r in self.rules:
            add(("owner", r.owner_id, "OWNS", "data_quality_rule", r.rule_id))
            add(("data_quality_rule", r.rule_id, "MONITORED_BY", "system", "S-DQM"))
            for c in r.cde_ids:
                add(("data_quality_rule", r.rule_id, "CHECKS", "cde", c))
            for col in r.columns:
                add(("data_quality_rule", r.rule_id, "APPLIES_TO", "database_column", col))
            for t in r.tables:
                add(("data_quality_rule", r.rule_id, "APPLIES_TO", "database_table", t))
            if r.feed_id:
                add(("data_quality_rule", r.rule_id, "COVERS", "feed", r.feed_id))
            for line in r.report_lines:
                add(("data_quality_rule", r.rule_id, "REACHES", "report_line", line))
            for rel in r.related:
                add(("data_quality_rule", r.rule_id, "RELATED_TO", "data_quality_rule", rel))
            for t in (r.warning, r.error):
                add(("dq_threshold", t.threshold_id, "OF", "data_quality_rule", r.rule_id))
        for line in self.report_lines:
            add(("report_line", line.line_id, "PART_OF", "regulatory_report", line.report_id))
            add(("report_line", line.line_id, "HAS_MDRM", "mdrm", line.mdrm))
        for m in self.mdrms:
            add(("mdrm", m.code, "DEFINED_FOR", "regulatory_report", m.report_id))
        for d in self.calendar:
            add(("calendar_day", d.day, "IN", "business_calendar", CALENDAR_ID))
        for i in self.issues:
            add(("issue", i.issue_id, "OWNED_BY", "owner", i.owner_id))
            for kind, key in i.concerns:
                add(("issue", i.issue_id, "CONCERNS", kind, key))
            for f in i.tracks:
                add(("issue", i.issue_id, "TRACKS", "examination_finding", f))
        for f in self.findings:
            for kind, key in f.concerns:
                add(("examination_finding", f.finding_id, "CONCERNS", kind, key))
        for g in self.exception_groups:
            add(("exception_group", g.group_id, "RAISED_BY", "data_quality_rule", g.rule_id))
            add(("exception_group", g.group_id, "ON_COB", "calendar_day", g.cob))
            add(("exception_group", g.group_id, "AFFECTS", "database_column", g.column))
            add(("exception_group", g.group_id, "EMITTED_BY", "system", "S-DQM"))
        for i in self.incidents:
            add(("incident", i.incident_id, "CONCERNS", "data_quality_rule", i.rule_id))
            add(("incident", i.incident_id, "ASSIGNED_TO", "owner", i.owner_id))
            for c in i.cde_ids:
                add(("incident", i.incident_id, "CONCERNS", "cde", c))
            if i.pattern_id:
                add(("incident", i.incident_id, "CAUSED_BY", "resolved_pattern", i.pattern_id))
            for other in i.correlated_with:
                add(("incident", i.incident_id, "CORRELATED_WITH", "incident", other))
            if i.raised_from:
                add(("incident", i.incident_id, "RAISED_FROM", "exception_group", i.raised_from))
        for p in self.precedents:
            add(("precedent", p.precedent_id, "FROM_INCIDENT", "incident", p.incident_id))
            add(("precedent", p.precedent_id, "MATCHES", "resolved_pattern", p.pattern_id))
        for p in self.patterns:
            for rid in p.rule_ids:
                add(("resolved_pattern", p.pattern_id, "SEEN_ON", "data_quality_rule", rid))
            template = remediation_template_id(p.pattern_id)
            add(("resolved_pattern", p.pattern_id, "IN_CATEGORY", "root_cause_category", p.family))
            add(("resolved_pattern", p.pattern_id, "FIXED_BY", "remediation_template", template))
            add(("remediation_template", template, "FOR_CATEGORY", "root_cause_category", p.family))
        return e

    def monitor_payload(self, group_id: str) -> dict[str, Any]:
        """The exception group as the DQ Monitor publishes it (what the Sentinel step receives)."""
        g = self.exception_group(group_id)
        return {"group_id": g.group_id, "monitor_rule_id": g.monitor_rule_id, "rule_id": g.rule_id,
                "rule_version": g.rule_version, "cob": g.cob, "platform": g.platform,
                "table": g.table, "column": g.column, "grade": g.monitor_grade,
                "metric": g.metric, "measured": g.measured, "failed": g.failed,
                "population": g.population, "thresholds": dict(g.monitor_thresholds),
                "sample_keys": list(g.sample_keys), "emitted_at": g.emitted_at}


def _exception_groups(rules: dict[str, DqRule], rng: random.Random) -> tuple[ExceptionGroup, ...]:
    out = []
    for gid, rid, cob, col, pop, failed, measured, override, emitted, note in _GROUP_ROWS:
        r = rules[rid]
        if measured is not None:
            value = measured
        elif r.warning.metric == "count":
            value = failed
        else:
            value = round(failed / pop, 6)
        thr = override or {"warning": r.warning.value, "error": r.error.value}
        table = col.rsplit(".", 1)[0]
        platform = next(t[2] for t in _TABLE_SPECS if t[0] == table)
        prefix = {"m_loan": "LN", "m_loan_transaction": "TX", "m_loan_repayment_schedule": "SC",
                  "m_loan_arrears_aging": "LN", "m_loan_charge": "CH", "m_client": "CL"}.get(
            table, "ROW")
        keys = tuple(sorted(f"{prefix}-{rng.randrange(1, 999999):06d}"
                            for _ in range(min(failed, 5))))
        out.append(ExceptionGroup(gid, r.monitor_id, rid, r.version, cob, platform, table, col, pop,
                                  failed, r.warning.metric, value, grade(r, value, thr), thr,
                                  emitted, keys, note))
    return tuple(out)


def _profiles(rules: tuple[DqRule, ...], rng: random.Random) -> tuple[PlatformProfile, ...]:
    platforms = ("S-LSP", "S-GL", "S-CIF", "S-EDW", "S-RRH", "S-COL", "S-DQM")
    volume = {"S-LSP": 1_240_000, "S-GL": 310_000, "S-CIF": 52_000, "S-EDW": 2_900_000,
              "S-RRH": 1_800, "S-COL": 4_100, "S-DQM": 96}
    out = []
    for p in platforms:
        prules = {}
        for r in rules:
            if r.system_id != p or r.warning.metric not in ("rate", "count"):
                continue
            base = r.warning.value * rng.uniform(0.05, 0.4) if r.warning.metric == "rate" \
                else round(rng.uniform(0, 0.4), 2)
            if r.rule_id == "NPB-DQ-CL-004":
                base = 0.00038   # the false positive: the data is at its usual rate
            last7 = [round(base * rng.uniform(0.7, 1.3), 6) for _ in range(7)]
            prules[r.rule_id] = {"baseline_rate_30d": round(base, 6),
                                 "stdev_30d": round(base * 0.2, 6), "last_7_cob": last7}
        out.append(PlatformProfile(p, volume[p], prules,
                                   tuple(c for c in CHANGES if c.system_id == p)))
    return tuple(out)


@lru_cache(maxsize=8)
def generate(seed: int = SEED) -> Dataset:
    """The whole world. Same seed, same world (the seed only moves sample keys and baselines)."""
    rng = random.Random(seed)
    rules = _build_rules()
    by_id = {r.rule_id: r for r in rules}
    return Dataset(
        seed=seed, owners=OWNERS, systems=SYSTEMS, structures=STRUCTURES, tables=_tables(),
        feeds=FEEDS, lineage=LINEAGE, cdes=CDES, rules=rules, retired_rules=RETIRED_RULES,
        reports=REPORTS, report_lines=REPORT_LINES, mdrms=MDRMS, calendar=_calendar(),
        incidents=INCIDENTS, patterns=PATTERNS, precedents=PRECEDENTS, issues=ISSUES,
        findings=FINDINGS, exception_groups=_exception_groups(by_id, rng),
        profiles=_profiles(rules, rng), changes=CHANGES)


def answer_key(seed: int = SEED) -> dict[str, Any]:
    """Ground truth for tests and evaluation. Never shown to an agent.

    {"exception_groups": {group id: {rulebook_grade, monitor_grade, incident_action
       ("open"|"attach"|"ignore"), attach_to, root_cause_family, precedent, pattern, lane,
       loop_back, ...}},
     "rules": {rule id: every rulebook field an extractor should recover},
     "retired_rules": [ids an extractor must NOT propose as live rules],
     "cdes": {cde id: {name, columns, reported_in}}}"""
    ds = generate(seed)
    groups = {}
    for g in ds.exception_groups:
        r = ds.rule(g.rule_id)
        groups[g.group_id] = {"rule_id": g.rule_id, "monitor_rule_id": g.monitor_rule_id,
                              "cob": g.cob, "rulebook_grade": grade(r, g.measured),
                              "monitor_grade": g.monitor_grade, **_EXPECTED[g.group_id]}
    rules = {r.rule_id: {
        "version": r.version, "monitor_id": r.monitor_id, "family": r.family, "title": r.title,
        "statement": r.statement, "dimension": r.dimension, "cde_ids": list(r.cde_ids),
        "columns": list(r.columns), "tables": list(r.tables), "check": r.check,
        "warning": r.warning.text, "error": r.error.text, "metric": r.warning.metric,
        "warning_value": r.warning.value, "error_value": r.error.value, "severity": r.severity,
        "owner_id": r.owner_id, "system_id": r.system_id, "feed_id": r.feed_id,
        "report_lines": list(r.report_lines),
        "mdrm": [ds.report_line(x).mdrm for x in r.report_lines], "related": list(r.related),
        "legal_reference": r.legal_reference} for r in ds.rules}
    return {"exception_groups": groups, "rules": rules,
            "retired_rules": [rid for rid, _, _ in ds.retired_rules],
            "cdes": {c.cde_id: {"name": c.name, "columns": list(c.columns),
                                "reported_in": list(c.reported_in)} for c in ds.cdes}}
