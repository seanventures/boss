# NorthPeak Bank loan data-quality sample (`tools/dq_sample`)

The synthetic world behind the **banking DQ case study**: the DQ incident pipeline
(Incident Intake → Context Gathering → Past Incidents → Root Cause Analysis → Cause Checks → Fix Planner → Close and Learn)
running on BOSS against a bank's loan book whose core is Apache Fineract.

Pure Python standard library, **deterministic**: no network, no model, no clock. The rulebook PDF
renderer needs Playwright Chromium; nothing else does.

| File | What it is |
|---|---|
| `data.py` | The single source of truth: Knowledge Graph and Context Graph seed, DQ Monitor exception groups, Profile Service profiles, and the answer key. |
| `rulebook.py` | Renders *NorthPeak Bank: Loan Data Quality Rulebook v3.2* from `data.py` (HTML → PDF with Playwright Chromium). |
| `NorthPeak_Loan_DQ_Rulebook_v3.2.pdf` | The rendered rulebook (45 Letter pages, about 400 KB, real text). Committed. |
| `engine/tests/test_dq_sample_data.py` | Determinism, counts, referential integrity, answer key and PDF round-trip. |

## API

```python
from tools.dq_sample import data as D

ds = D.generate()            # Dataset; cached; generate(seed=...) moves only sample keys and baselines
key = D.answer_key()         # ground truth, never shown to an agent
D.grade(rule, measured)      # "Error" | "Warning" | "Clean" against the rule's own thresholds
```

### `Dataset` fields (all tuples of frozen dataclasses)

| Field | Type | Count |
|---|---|---|
| `owners` | `Owner` (teams with Service Desk assignment groups, and people with `team_id`) | 20 |
| `systems` | `System` (S-LSP Fineract, S-GL, S-PAY, S-CIF, S-COL, S-CLT, S-EDW, S-RRH, S-DQM DQ Monitor, S-DESK Service Desk, S-PRF Profile Service) | 11 |
| `structures` | `DatabaseStructure` (schemas) | 8 |
| `tables` | `DatabaseTable` (with `columns: tuple[DatabaseColumn]`, `fineract`, `in_code_graph`) | 28 tables, 173 columns |
| `feeds` | `Feed` F-01..F-12 (`reads`, `feeds` table keys) | 12 |
| `lineage` | `Lineage` column→column hops, and extract column→report line hops | 41 |
| `cdes` | `Cde` CDE-LN-001..CDE-PV-001 (columns, steward, upstream CDEs, report lines) | 24 |
| `rules` | `DqRule` NPB-DQ-&lt;LN/TX/SC/AR/CH/CL/GL/RP/FD&gt;-nnn, each with `warning` and `error` `DqThreshold` | 96 (+4 retired in `retired_rules`) |
| `reports`, `report_lines`, `mdrms` | Call Report (FFIEC 031) and FR Y-9C lines with MDRM codes (**illustrative**) | 2, 21, 21 |
| `calendar` | `CalendarDay` 2026-09-01..2026-12-31 (US Federal Reserve holidays, quarter-ends) | 122 |
| `incidents` | `Incident`: 10 closed (confirmed cause, ruled out, fix, fix durability) + 1 open (INC-2301) | 11 |
| `patterns`, `precedents` | `ResolvedPattern` (one per root-cause pattern), `Precedent` (one per closed incident) | 8, 10 |
| `issues`, `findings` | `Issue`, `ExaminationFinding` | 5, 5 |
| `exception_groups` | `ExceptionGroup` as the DQ Monitor emits them, COB 2026-10-01..2026-10-07 | 13 |
| `profiles`, `changes` | Profile Service `PlatformProfile` per platform (baseline failure rate per rule, recent `Change`s) | 7, 8 |

Lookups: `ds.rule(id)`, `ds.rule_by_monitor_id("DQX-0588")`, `ds.cde(id)`, `ds.table(key)`,
`ds.column("m_loan.principal_outstanding_derived")`, `ds.system(id)`, `ds.feed(id)`, `ds.owner(id)`,
`ds.report_line(id)`, `ds.incident(id)`, `ds.exception_group(id)`, `ds.profile("S-LSP")`,
`ds.calendar_day("2026-10-05")`, `ds.rules_in_family("LN")`, `ds.open_incidents()`,
`ds.lineage_upstream(column_or_line)` (every registered hop upstream of a key).

Graph projection (what a loader writes):
- `ds.nodes()` → `[{"kind", "key", "name", "attrs"}]` (attrs are JSON-safe)
- `ds.edges()` → `[(source_kind, source_key, RELATIONSHIP, target_kind, target_key)]`
- `ds.monitor_payload(group_id)` → the exception group exactly as the DQ Monitor mock should serve it.

Constants: `KG_KINDS`, `CONTEXT_KINDS`, `RELATIONSHIPS`, `DIMENSIONS`, `ROOT_CAUSE_FAMILIES`,
`LANES`, `FAMILIES`, `KNOWN_LINEAGE_GAPS`, `CODE_GRAPH_TABLES`, `RUNNING_EXAMPLE`
(`EXG-20261005-0003`), `MDRM_DISCLAIMER`.

### Node keys

| Kind | Key example |
|---|---|
| system / owner / feed / lineage | `S-LSP` / `OWN-LOANOPS`, `P-MFELD` / `F-01` / `LIN-01` |
| database_structure / database_table / database_column | `DS-LSP-FINERACT` / `m_loan` (Fineract tables keep their plain name, matching the code graph's `table` nodes), `edw.fact_loan_daily` / `m_loan.principal_outstanding_derived` |
| cde / data_quality_rule / dq_threshold | `CDE-LN-001` / `NPB-DQ-LN-003` / `NPB-DQ-LN-003:warning`, `NPB-DQ-LN-003:error` |
| regulatory_report / report_line / mdrm | `FFIEC-031`, `FR-Y-9C` / `CR-RCC-12` / `RCON2122` |
| business_calendar / calendar_day | `NPB-US-2026` / `2026-10-05` |
| issue / examination_finding | `ISS-031` / `EF-2025-02` |
| exception_group / incident / precedent / resolved_pattern | `EXG-20261005-0003` / `INC-2187` / `PRE-2187` / `RPAT-COB-ORDER` |

## Relationships

Every edge `edges()` emits uses one of these (the test enforces it); the ontology pack mirrors them.

| Source kind | Relationship | Target kind | Meaning |
|---|---|---|---|
| owner | OWNS | system | the team/person accountable for a system |
| owner | OWNS | feed | the team that runs a feed |
| owner | OWNS | data_quality_rule | the rule owner (Service Desk tickets go to its group) |
| owner | STEWARDS | cde | the data steward of a critical data element |
| owner | MEMBER_OF | owner | a person belongs to a team |
| system | HOSTS | database_structure | a system hosts a database/schema |
| system | OWNS | database_table | the system of record for a table |
| system | USES_CALENDAR | business_calendar | the calendar a system's COB follows |
| database_structure | CONTAINS | database_table | a schema contains a table |
| database_table | HAS_COLUMN | database_column | a table has a column |
| feed | FROM_SYSTEM | system | a feed's source system |
| feed | TO_SYSTEM | system | a feed's target system |
| feed | READS | database_table | a feed extracts from a table |
| feed | FEEDS | database_table | a feed loads a table |
| lineage | FROM | database_column | a lineage hop starts at a column |
| lineage | TO | database_column | a lineage hop lands in a column |
| lineage | TO | report_line | the last hop lands on a report line |
| lineage | VIA | feed | the feed that carries a hop |
| cde | STORED_IN | database_column | where a CDE's value of record lives |
| cde | REPORTED_IN | report_line | a report line the CDE feeds |
| cde | DERIVED_FROM | cde | upstream CDE (business lineage) |
| cde | SYSTEM_OF_RECORD | system | the CDE's system of record |
| data_quality_rule | CHECKS | cde | the CDE a rule protects |
| data_quality_rule | APPLIES_TO | database_column | a column the check reads |
| data_quality_rule | APPLIES_TO | database_table | the table the check runs on |
| data_quality_rule | COVERS | feed | the feed whose output the rule checks |
| data_quality_rule | MONITORED_BY | system | the DQ monitor that runs it (the DQ Monitor) |
| data_quality_rule | REACHES | report_line | regulatory reach of the rule |
| data_quality_rule | RELATED_TO | data_quality_rule | a related rule |
| dq_threshold | OF | data_quality_rule | a rule's Warning or Error threshold |
| report_line | PART_OF | regulatory_report | a line belongs to a report |
| report_line | HAS_MDRM | mdrm | the MDRM code of a report line |
| mdrm | DEFINED_FOR | regulatory_report | the report an MDRM code belongs to |
| calendar_day | IN | business_calendar | a day in a calendar |
| issue | CONCERNS | system / feed / data_quality_rule / database_table | a known issue affects it |
| issue | TRACKS | examination_finding | the issue that remediates a finding |
| issue | OWNED_BY | owner | who works the issue |
| examination_finding | CONCERNS | report_line / data_quality_rule / cde / feed / business_calendar | a finding cites it |
| exception_group | RAISED_BY | data_quality_rule | the rule whose check failed |
| exception_group | ON_COB | calendar_day | the close of business graded |
| exception_group | AFFECTS | database_column | the failing column |
| exception_group | EMITTED_BY | system | the DQ monitor that emitted it (the DQ Monitor) |
| incident | RAISED_FROM | exception_group | the exception group that opened it |
| incident | CONCERNS | data_quality_rule / cde | what the incident is about |
| incident | CAUSED_BY | resolved_pattern | the confirmed root-cause pattern |
| incident | ASSIGNED_TO | owner | the owner working the incident |
| incident | CORRELATED_WITH | incident | a recurrence of an earlier incident |
| precedent | FROM_INCIDENT | incident | the closed incident a precedent records |
| precedent | MATCHES | resolved_pattern | the pattern a precedent confirms |
| resolved_pattern | SEEN_ON | data_quality_rule | a rule the pattern has broken |

The Profile Service profiles (`profiles`, `changes`) are served by the profile-service mock, not written
as graph nodes.

## The planted exception groups (answer key)

| Group | Rule (DQ Monitor id) | COB | Grade | Expected |
|---|---|---|---|---|
| EXG-20261001-0001 | TX-006 (DQX-0611) | 10-01 | Warning | ignore (dashboard only) |
| EXG-20261001-0003 | GL-002 v2 (DQX-0707) | 10-01 | Error | open; reference_data; precedent INC-2112; change CHG-7702 |
| EXG-20261002-0002 | AR-001 v2 (DQX-0646) | 10-02 | Error | open (became INC-2301, still open); feed_etl; precedent INC-2240 |
| EXG-20261002-0004 | CH-001 (DQX-0666) | 10-02 | Warning | ignore |
| **EXG-20261005-0003** | **LN-003 v2 (DQX-0588)** | **10-05** | **Error** | **open; source_processing; precedent INC-2187 (and INC-2041), pattern RPAT-COB-ORDER; recent change CHG-7731 regressed the INC-2187 fix** |
| EXG-20261005-0005 | AR-001 v2 | 10-05 | Error | **attach** to open INC-2301 |
| EXG-20261006-0001 | FD-001 (DQX-0746) | 10-06 | Error | open; feed_etl; precedent INC-1988 |
| EXG-20261006-0002 | CL-004 v3 (DQX-0689) | 10-06 | DQ Monitor Error / rulebook Warning | **false positive**: the DQ Monitor's thresholds 100x too tight after CHG-7745; precedent INC-2203; cause check `rule_drafting` |
| EXG-20261006-0005 | SC-004 (DQX-0629) | 10-06 | Error | open; source_processing; precedent INC-2059 (short payment, Reg Z) |
| EXG-20261006-0007 | LN-003 v2 (DQX-0588) | 10-06 | Error | attach to the incident opened for EXG-20261005-0003 |
| EXG-20261007-0001 | CL-001 (DQX-0686) | 10-07 | Error | open; manual_entry; precedent INC-2150 |
| EXG-20261007-0004 | RP-006 (DQX-0731) | 10-07 | Error | open; **insufficient evidence**: the EDW → rrh.fry9c_extract hop (F-06) is not registered (EF-2025-02, ISS-044); RCA loops back |
| EXG-20261007-0006 | TX-009 v2 (DQX-0614) | 10-07 | Warning | ignore |

`answer_key()["rules"]` holds every rulebook field per rule id (title, statement, dimension, CDEs,
columns, check, warning/error text and values, owner, feed, report lines, MDRM codes, legal
reference) for measuring rulebook extraction; `answer_key()["retired_rules"]` lists the four
Appendix C rules an extractor must not propose.

## Notes on realism

- Fineract tables and columns use Apache Fineract's schema names (`m_loan.principal_outstanding_derived`,
  `m_loan_transaction.amount`, `m_loan_repayment_schedule.completed_derived`,
  `m_loan_arrears_aging.overdue_since_date_derived`, `m_loanproduct_provisioning_entry.reseve_amount`
  (sic) ...). The northpeak-lending code graph has `table` nodes but no column nodes, so the column
  names could not be cross-checked there; `DatabaseTable.in_code_graph` says which tables match a
  code-graph `table` node.
- Report line numbers and MDRM codes are **illustrative**, following the public FFIEC 031 and
  FR Y-9C schedules; the rulebook front matter and Appendix A say to verify them against the FFIEC
  MDRM dictionary.
- The bank, people and figures are fictitious.

## Regenerate the PDF

```bash
python3 -c "import playwright"                       # Playwright + Chromium (console tests use it)
python3 -m tools.dq_sample.rulebook                  # writes tools/dq_sample/NorthPeak_Loan_DQ_Rulebook_v3.2.pdf
python3 -m tools.dq_sample.rulebook --html /tmp/rb.html   # also keep the HTML
PYTHONPATH=.:engine python3 -m pytest engine/tests/test_dq_sample_data.py -q
```

Rendering is deterministic on a given machine (fixed font stack, ligatures and hyphenation off,
CreationDate/ModDate pinned to 2026-07-01): two renders are byte-identical. Fonts are the
system's Helvetica/Arial, so another machine may produce slightly different bytes with the same
text. Change `data.py`, re-render, and commit both; the test fails if the PDF's text no longer
matches the data.
