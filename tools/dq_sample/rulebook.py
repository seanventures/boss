"""Render "NorthPeak Bank: Loan Data Quality Rulebook v3.2" as a genuine PDF from tools/dq_sample/data.py.

    python3 -m tools.dq_sample.rulebook            # writes tools/dq_sample/NorthPeak_Loan_DQ_Rulebook_v3.2.pdf
    python3 -m tools.dq_sample.rulebook --html out.html   # also keep the HTML

HTML -> PDF with Playwright's Chromium (print to PDF). Deterministic: no timestamps in the content,
fixed font stack, ligatures and hyphenation off, and the PDF's CreationDate/ModDate are pinned to
the rulebook's effective date after printing. Rule cards are labelled lines (not table cells), so
text extraction (pypdf) reads each card in order.
"""
from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

from tools.dq_sample import data as D

OUT = Path(__file__).with_name("NorthPeak_Loan_DQ_Rulebook_v3.2.pdf")


def _e(s: object) -> str:
    return html.escape(str(s), quote=False)


def _owner(ds: D.Dataset, owner_id: str) -> str:
    return ds.owner(owner_id).name


def _line_ref(ds: D.Dataset, line_id: str) -> str:
    ln = ds.report_line(line_id)
    form = "Call Report" if ln.report_id == "FFIEC-031" else "FR Y-9C"
    return f"{form} {ln.schedule} item {ln.item} ({ln.mdrm})"


CSS = """
@page { size: Letter; margin: 22mm 20mm 22mm 20mm; }
* { box-sizing: border-box; }
html { font-family: "Helvetica Neue", Helvetica, Arial, "Liberation Sans", sans-serif;
       font-size: 10pt; color: #1b2430; line-height: 1.42;
       font-variant-ligatures: none; font-kerning: none; hyphens: none; }
h1, h2, h3 { color: #12355b; font-weight: 700; margin: 0; }
h1.chapter { font-size: 19pt; border-bottom: 2px solid #12355b; padding-bottom: 6pt;
             margin: 0 0 12pt 0; break-before: page; }
h2 { font-size: 12.5pt; margin: 14pt 0 6pt 0; }
h3 { font-size: 10.5pt; margin: 10pt 0 4pt 0; }
p { margin: 0 0 7pt 0; }
ul { margin: 0 0 8pt 0; padding-left: 16pt; }
li { margin: 0 0 3pt 0; }
table { width: 100%; border-collapse: collapse; margin: 4pt 0 10pt 0; font-size: 8.6pt; }
th { background: #12355b; color: #fff; text-align: left; padding: 4pt 5pt; font-weight: 600; }
td { border-bottom: 0.5pt solid #c9d2dc; padding: 3.5pt 5pt; vertical-align: top; }
tr { break-inside: avoid; }
.cover { height: 230mm; display: flex; flex-direction: column; justify-content: space-between; }
.cover .band { background: #12355b; color: #fff; padding: 26pt 24pt; }
.cover .bank { font-size: 13pt; letter-spacing: 2pt; text-transform: uppercase; opacity: .85; }
.cover .title { font-size: 28pt; font-weight: 700; margin-top: 10pt; line-height: 1.15; }
.cover .version { font-size: 14pt; margin-top: 10pt; }
.cover dl { display: grid; grid-template-columns: 42mm 1fr; gap: 4pt 10pt; font-size: 10pt; }
.cover dt { font-weight: 700; color: #12355b; }
.cover dd { margin: 0; }
.notice { border: 1pt solid #b8860b; background: #fdf6e3; padding: 8pt 10pt; font-size: 9pt;
          margin: 8pt 0 10pt 0; break-inside: avoid; }
.toc li { margin: 0 0 4pt 0; }
.card { border: 0.8pt solid #c9d2dc; border-left: 4pt solid #12355b; padding: 5pt 9pt 4pt 9pt;
        margin: 0 0 7pt 0; break-inside: avoid; font-size: 8.5pt; line-height: 1.3; }
.card .rid { font-size: 9.8pt; font-weight: 700; color: #12355b; margin-bottom: 2.5pt; }
.card p { margin: 0 0 1.5pt 0; }
h1.family { font-size: 15pt; border-bottom: 1.5px solid #12355b; padding-bottom: 4pt;
            margin: 16pt 0 8pt 0; break-after: avoid; }
h1.family.first { break-before: page; margin-top: 0; }
.famintro { break-after: avoid; }
.card b { color: #12355b; }
.sev-High { border-left-color: #a61b1b; }
.sev-Medium { border-left-color: #c77700; }
.sev-Low { border-left-color: #4a7a3d; }
.cde { border-bottom: 0.5pt solid #c9d2dc; padding: 5pt 0; break-inside: avoid; font-size: 9.2pt; }
.cde p { margin: 0 0 2pt 0; }
.small { font-size: 8.6pt; color: #4a5563; }
.chip { display: inline-block; border: 0.6pt solid #12355b; border-radius: 3pt; padding: 0 4pt;
        font-size: 8pt; margin-right: 3pt; }
"""


def _cover(ds: D.Dataset) -> str:
    return f"""
<section class="cover">
  <div class="band">
    <div class="bank">{_e(D.BANK)}</div>
    <div class="title">Loan Data Quality Rulebook</div>
    <div class="version">Version {D.RULEBOOK_VERSION} &middot; Effective {D.RULEBOOK_EFFECTIVE}</div>
  </div>
  <dl>
    <dt>Document owner</dt><dd>Chief Data Office ({_e(ds.owner('P-DWHITFIELD').name)}, Chief Data Officer)</dd>
    <dt>Approved by</dt><dd>Data Governance Council, 2026-06-18</dd>
    <dt>Applies to</dt><dd>Loan servicing, ledger, warehouse and regulatory reporting data for the
      consumer and residential loan book</dd>
    <dt>Rules in force</dt><dd>{len(ds.rules)} rules in {len(D.FAMILY_ORDER)} families;
      {len(ds.cdes)} critical data elements</dd>
    <dt>Monitored by</dt><dd>Eagle DQ Monitor (S-EGL), graded at each close of business</dd>
    <dt>Classification</dt><dd>Internal. SYNTHETIC DOCUMENT prepared for a demonstration; the bank,
      people and figures are fictitious.</dd>
  </dl>
</section>"""


def _toc() -> str:
    items = ["0. About this rulebook", "1. Severity and grading", "2. Governance",
             "3. Systems register", "4. Feeds and lineage register",
             "5. Critical data element register"]
    for i, fam in enumerate(D.FAMILY_ORDER, 6):
        items.append(f"{i}. {D.FAMILIES[fam][0]} rules (NPB-DQ-{fam})")
    items += ["Appendix A. Report line and MDRM crosswalk", "Appendix B. Business calendar",
              "Appendix C. Change log and retired rules", "Appendix D. Glossary"]
    lis = "".join(f"<li>{_e(x)}</li>" for x in items)
    return f'<h1 class="chapter">Contents</h1><ul class="toc">{lis}</ul>'


def _about(ds: D.Dataset) -> str:
    hist = "".join(f"<tr><td>v{v}</td><td>{d}</td><td>{_e(t)}</td></tr>"
                   for v, d, t in D.VERSION_HISTORY)
    return f"""
<h1 class="chapter">0. About this rulebook</h1>
<h2>0.1 Purpose</h2>
<p>This rulebook sets out the data quality rules that protect the critical data elements (CDEs) of
{_e(D.BANK)}'s loan book, from the Loan Servicing Platform through the Enterprise Data Warehouse to
the figures the bank files on the Call Report (FFIEC 031) and the FR Y-9C. Each rule states what
good data looks like, how it is checked, when a failure is a Warning and when it is an Error, who
owns it, and which regulatory lines depend on it.</p>
<p>It is the reference the Eagle DQ Monitor is configured from, and the reference incident
investigators use to judge whether an exception is a real data problem, a known recurring pattern,
or a fault in the rule itself.</p>
<h2>0.2 Scope</h2>
<ul>
<li>Systems: the Loan Servicing Platform (Apache Fineract), General Ledger, Payments Hub, Customer
Master, Collections, Collateral Management, Enterprise Data Warehouse and Regulatory Reporting Hub.</li>
<li>Data: loan accounts, repayment schedules, loan transactions, arrears aging, loan charges,
clients, loan accounting journals, provisioning, and the quarterly regulatory extracts.</li>
<li>Out of scope: deposits, treasury, and the commercial loan book (covered by the Commercial
Lending DQ Rulebook).</li>
</ul>
<h2>0.3 Ownership and approval</h2>
<p>The Chief Data Office owns this rulebook. Each rule has a rule owner (a business team), and each
CDE has a data steward who approves the thresholds of every rule that checks it. Changes follow the
process in chapter 2 and are approved by the Data Governance Council.</p>
<h2>0.4 Version history</h2>
<table><tr><th>Version</th><th>Effective</th><th>Change</th></tr>{hist}</table>
<h2>0.5 How to read a rule card</h2>
<p>Every rule is printed as a card of labelled lines, in this order:</p>
<ul>
<li><b>Rule id, version and title.</b> Rule ids take the form NPB-DQ-&lt;family&gt;-&lt;nnn&gt; and
never change; the version rises when the check or a threshold changes.</li>
<li><b>Statement:</b> the rule in plain language.</li>
<li><b>Dimension, Severity, System, Feed, Eagle rule:</b> the data quality dimension, the business
impact of an Error, the system of record checked, the feed whose output is checked, and the id the
rule carries in Eagle.</li>
<li><b>CDE</b> and <b>Applies to:</b> the critical data elements protected and the table.column
values the check reads.</li>
<li><b>Check:</b> the check, in words.</li>
<li><b>Warning when</b> and <b>Error when:</b> the rule's own grading thresholds (chapter 1).</li>
<li><b>Regulatory reach:</b> report lines and MDRM codes that depend on the data.</li>
<li><b>Owner, Handling, Related:</b> who acts, what happens on an Error, and related rules.</li>
</ul>
<div class="notice"><b>Regulatory references.</b> {_e(D.MDRM_DISCLAIMER)}</div>
"""


def _grading(ds: D.Dataset) -> str:
    return """
<h1 class="chapter">1. Severity and grading</h1>
<p>Eagle runs every rule at each close of business (COB) and grades the result against the rule's
own thresholds. The grading unit is one rule version on one COB: Eagle publishes one exception
group per failing rule per COB.</p>
<h2>1.1 Warning and Error</h2>
<ul>
<li><b>Warning</b> goes to the DQ dashboard only. No incident is opened and the Monitoring step
writes nothing.</li>
<li><b>Error</b> opens an incident for the rule owner, or attaches to the open incident for the same
rule and cause. An attach does not restart the investigation.</li>
<li>A rule whose Warning line reads "never" has no Warning grade: any failure is an Error.</li>
</ul>
<h2>1.2 Severity only moves up</h2>
<p>An incident's severity is set when it opens and can only rise: a later Error on the same rule
raises it, a later Warning or clean COB never lowers it. Closing an incident is the only way out.</p>
<h2>1.3 Metrics</h2>
<table>
<tr><th>Metric</th><th>Measured as</th><th>Example</th></tr>
<tr><td>Failure rate</td><td>failing records divided by the population at COB</td>
<td>more than 0.25% of active loans fail at COB</td></tr>
<tr><td>Failure count</td><td>number of failing records at COB</td>
<td>more than 5 active loans fail at COB</td></tr>
<tr><td>Relative difference</td><td>|A &minus; B| / B for two totals that must agree</td>
<td>the two totals differ by more than 0.05%</td></tr>
<tr><td>Amount difference</td><td>|A &minus; B| in US dollars</td>
<td>the two totals differ by more than USD 25,000</td></tr>
<tr><td>Lateness</td><td>minutes after a feed's or job's deadline</td>
<td>the deadline is missed by more than 60 minutes</td></tr>
</table>
<h2>1.4 Close of business</h2>
<p>COB follows the NorthPeak US business calendar (Appendix B): weekdays except Federal Reserve
holidays. A rule that runs on a non-business day is not graded. Quarterly rules (family RP) are
graded on the extract built for the quarter-end COB.</p>
<h2>1.5 False positives</h2>
<p>When an investigation concludes that the data is correct and the rule or its configuration is
wrong, the incident is closed as a false positive and a Rule Drafting case is opened against this
rulebook. Thresholds are never changed in Eagle alone.</p>
"""


def _governance(ds: D.Dataset) -> str:
    teams = [o for o in ds.owners if o.owner_type == "team"]
    rows = "".join(
        f"<tr><td>{_e(o.name)}</td><td>{_e(o.title)}</td><td>{_e(o.itsm_group)}</td>"
        f"<td>{_e(', '.join(p.name for p in ds.owners if p.team_id == o.owner_id))}</td></tr>"
        for o in teams)
    findings = "".join(
        f"<tr><td>{f.finding_id}</td><td>{_e(f.severity)}</td><td>{_e(f.title)}</td>"
        f"<td>{_e(f.status)}</td><td>{f.due}</td></tr>" for f in ds.findings)
    return f"""
<h1 class="chapter">2. Governance</h1>
<h2>2.1 Roles</h2>
<ul>
<li><b>Chief Data Office</b> owns the rulebook and chairs the Data Governance Council.</li>
<li><b>Data stewards</b> approve the definition of each CDE and the thresholds of every rule that
checks it (remediation of EF-2024-11).</li>
<li><b>Rule owners</b> receive incidents for their rules through their ITSM assignment group and
own the fix.</li>
<li><b>Data Quality Operations</b> configures Eagle from this rulebook and may not change a
threshold without an approved rulebook change.</li>
<li><b>Investigators</b> (people or approved agents) may prepare a remediation; a person approves
it and a different person carries it out.</li>
</ul>
<h2>2.2 Owners and assignment groups</h2>
<table><tr><th>Team</th><th>Responsibility</th><th>ITSM group</th><th>Steward / lead</th></tr>
{rows}</table>
<h2>2.3 Changing a rule</h2>
<ul>
<li>A change is proposed as a Rule Drafting case with evidence (exception history, profile,
incident).</li>
<li>The CDE steward approves thresholds; the rule owner approves the check.</li>
<li>The version rises; Eagle is reconfigured from the new version; retired rules are listed in
Appendix C and never reused.</li>
</ul>
<h2>2.4 Open supervisory matters</h2>
<table><tr><th>Finding</th><th>Type</th><th>Title</th><th>Status</th><th>Due</th></tr>
{findings}</table>
"""


def _systems(ds: D.Dataset) -> str:
    rows = "".join(
        f"<tr><td>{s.system_id}</td><td>{_e(s.name)}</td><td>{s.tier}</td>"
        f"<td>{_e(_owner(ds, s.owner_id))}</td><td>{_e(s.description)}</td></tr>"
        for s in ds.systems)
    structs = "".join(
        f"<tr><td>{d.structure_id}</td><td>{_e(d.name)}</td><td>{d.system_id}</td>"
        f"<td>{_e(d.engine)}</td><td>{_e(', '.join(t.key for t in ds.tables if t.structure_id == d.structure_id))}</td></tr>"
        for d in ds.structures)
    return f"""
<h1 class="chapter">3. Systems register</h1>
<p>Tier 1 systems hold or transform data that reaches a regulatory filing.</p>
<table><tr><th>Id</th><th>System</th><th>Tier</th><th>Owner</th><th>Description</th></tr>{rows}</table>
<h2>3.1 Database structures and tables</h2>
<p>Loan platform tables keep their Apache Fineract names (for example m_loan,
m_loan_transaction, m_loan_repayment_schedule).</p>
<table><tr><th>Id</th><th>Schema</th><th>System</th><th>Engine</th><th>Tables</th></tr>{structs}</table>
"""


def _feeds(ds: D.Dataset) -> str:
    rows = "".join(
        f"<tr><td>{f.feed_id}</td><td>{_e(f.name)}</td><td>{_e(', '.join(f.reads))}</td>"
        f"<td>{_e(', '.join(f.feeds))}</td><td>{_e(f.frequency)}; {_e(f.sla)}</td>"
        f"<td>{_e(_owner(ds, f.owner_id))}</td></tr>" for f in ds.feeds)
    hops = "".join(
        f"<tr><td>{ln.lineage_id}</td><td>{_e(ln.from_column)}</td>"
        f"<td>{_e(_line_ref(ds, ln.to) if ln.to_kind == 'report_line' else ln.to)}</td>"
        f"<td>{ln.feed_id or '-'}</td><td>{_e(ln.transform)}</td></tr>"
        for ln in ds.lineage if ln.to_kind == "database_column")
    gaps = "".join(
        f"<li>{_e(g['from'])} to {_e(g['to'])} via {g['feed_id']}: not registered "
        f"(finding {g['finding_id']}, issue {g['issue_id']}).</li>" for g in D.KNOWN_LINEAGE_GAPS)
    return f"""
<h1 class="chapter">4. Feeds and lineage register</h1>
<table><tr><th>Feed</th><th>Name</th><th>Reads</th><th>Loads</th><th>Schedule and SLA</th><th>Owner</th></tr>{rows}</table>
<h2>4.1 Column lineage</h2>
<p>Registered column-level hops. Every extract column (rrh.call_report_extract.amount,
rrh.fry9c_extract.amount) is in turn filed on the report lines listed in Appendix A.</p>
<table><tr><th>Hop</th><th>From</th><th>To</th><th>Feed</th><th>Transform</th></tr>{hops}</table>
<h2>4.2 Known lineage gaps</h2>
<ul>{gaps}</ul>
"""


def _cde_register(ds: D.Dataset) -> str:
    blocks = []
    for c in ds.cdes:
        rep = "; ".join(_line_ref(ds, x) for x in c.reported_in) or "none (internal use)"
        blocks.append(
            f'<div class="cde"><p><b>{c.cde_id} {_e(c.name)}</b></p>'
            f"<p>Definition: {_e(c.definition)}</p>"
            f"<p>System of record: {c.system_of_record}. Stored in: {_e(', '.join(c.columns))}. "
            f"Steward: {_e(ds.owner(c.steward_id).name)}.</p>"
            f"<p>Upstream CDE: {_e(', '.join(c.upstream) or 'none')}. Reported in: {_e(rep)}.</p>"
            "</div>")
    return ('<h1 class="chapter">5. Critical data element register</h1>'
            "<p>Twenty-four CDEs. A CDE's thresholds are approved by its steward.</p>"
            + "".join(blocks))


def rule_card(ds: D.Dataset, r: D.DqRule) -> str:
    cdes = "; ".join(f"{c} {ds.cde(c).name} ({', '.join(ds.cde(c).columns)})" for c in r.cde_ids)
    reach = "; ".join(_line_ref(ds, x) for x in r.report_lines) or "none"
    owner = ds.owner(r.owner_id)
    steward = next((p.name for p in ds.owners if p.team_id == r.owner_id), "")
    lines = [
        f'<div class="rid">{r.rule_id} v{r.version} &mdash; {_e(r.title)}</div>',
        f"<p><b>Statement:</b> {_e(r.statement)}</p>",
        f"<p><b>Dimension:</b> {r.dimension.capitalize()} &nbsp; <b>Severity:</b> {r.severity} "
        f"&nbsp; <b>System:</b> {r.system_id} &nbsp; <b>Feed:</b> {r.feed_id or '-'} &nbsp; "
        f"<b>Eagle rule:</b> {r.eagle_id}</p>",
        f"<p><b>CDE:</b> {_e(cdes)}</p>",
        f"<p><b>Applies to:</b> {_e(', '.join(r.columns))}</p>",
        f"<p><b>Check:</b> {_e(r.check)}</p>",
        f"<p><b>Warning when:</b> {_e(r.warning.text)}</p>",
        f"<p><b>Error when:</b> {_e(r.error.text)}</p>",
        f"<p><b>Regulatory reach:</b> {_e(reach)}.</p>",
    ]
    if r.legal_reference:
        lines.append(f"<p><b>Legal reference:</b> {_e(r.legal_reference)}.</p>")
    lines += [
        f"<p><b>Owner:</b> {_e(owner.name)}" + (f" (steward {_e(steward)})" if steward else "")
        + f". &nbsp; <b>Related:</b> {_e(', '.join(r.related) or 'none')}.</p>",
        f"<p><b>Handling:</b> {_e(r.handling)}</p>",
    ]
    return f'<div class="card sev-{r.severity}">' + "".join(lines) + "</div>"


_FAMILY_INTRO = {
    "LN": "Loan master rules protect the balances, dates, rates and status held on m_loan, the "
          "source of total loans on RC-C and HC-C.",
    "TX": "Transaction rules protect how each payment, reversal, charge-off and recovery on "
          "m_loan_transaction is allocated, including partial-payment handling under Regulation Z.",
    "SC": "Schedule rules protect the installments on m_loan_repayment_schedule from which "
          "arrears and past-due status are derived.",
    "AR": "Arrears rules protect overdue-since dates and overdue amounts on m_loan_arrears_aging, "
          "which drive RC-N and HC-N past-due reporting.",
    "CH": "Charge rules protect fees and penalties on m_loan_charge.",
    "CL": "Customer rules protect client identity and age data on m_client, synchronised from "
          "the Customer Master.",
    "GL": "Ledger rules protect loan accounting on acc_gl_journal_entry and its reconciliation to "
          "the General Ledger.",
    "RP": "Reporting rules tie the quarterly Call Report and FR Y-9C extracts back to the "
          "warehouse and the transactions.",
    "FD": "Feed rules protect the timeliness and completeness of the daily feeds that every other "
          "family depends on.",
}


def _rule_chapters(ds: D.Dataset) -> str:
    out = []
    for i, fam in enumerate(D.FAMILY_ORDER, 6):
        name, table, owner, feed, pop, _ = D.FAMILIES[fam]
        rules = ds.rules_in_family(fam)
        cls = "family first" if i == 6 else "family"
        out.append(
            f'<h1 class="{cls}">{i}. {_e(name)} rules (NPB-DQ-{fam})</h1>'
            f'<p class="famintro">{_e(_FAMILY_INTRO[fam])} Rule owner by default: '
            f"{_e(_owner(ds, owner))}. Rules in this family: {len(rules)}.</p>"
            + "".join(rule_card(ds, r) for r in rules))
    return "".join(out)


def _appendices(ds: D.Dataset) -> str:
    xwalk = "".join(
        f"<tr><td>{ln.mdrm}</td><td>{'Call Report' if ln.report_id == 'FFIEC-031' else 'FR Y-9C'}</td>"
        f"<td>{_e(ln.schedule)}</td><td>{_e(ln.item)}</td><td>{_e(ln.caption)}</td>"
        f"<td>{_e(', '.join(r.rule_id for r in ds.rules if ln.line_id in r.report_lines))}</td></tr>"
        for ln in ds.report_lines)
    hol = "".join(f"<tr><td>{d}</td><td>{_e(n)}</td></tr>" for d, n in D.HOLIDAYS_2026.items())
    qe = ", ".join(d.day for d in ds.calendar if d.quarter_end)
    retired = "".join(f"<tr><td>{rid}</td><td>{_e(t)}</td><td>{_e(why)}</td></tr>"
                      for rid, t, why in ds.retired_rules)
    regraded = "".join(f"<li>{r.rule_id} v{r.version}: {_e(r.title)}</li>"
                       for r in ds.rules if r.version > 1)
    glossary = (
        ("Close of business (COB)", "The business day whose data is being graded."),
        ("Critical data element (CDE)", "A data element whose error would materially misstate a "
         "regulatory filing, financial statement or customer outcome."),
        ("Data quality dimension", "Completeness, validity, accuracy, consistency, timeliness or "
         "uniqueness."),
        ("Eagle", "The bank's DQ monitor; it runs each rule at COB and emits exception groups."),
        ("Exception group", "All failures of one rule version on one COB, as Eagle publishes them."),
        ("Feed", "A scheduled transfer of data between two systems."),
        ("ITSM", "The bank's service-management system for incidents, problems and changes."),
        ("Lineage", "The documented path of a value from its system of record to a report line."),
        ("MDRM", "Micro Data Reference Manual: the Federal Reserve's catalogue of reporting codes."),
        ("Olympus", "The profile service that keeps baseline failure rates and recent changes per "
         "data platform."),
        ("Precedent", "A closed incident whose confirmed cause and fix inform a new investigation."),
        ("Rule Drafting case", "A proposal to change this rulebook, opened when an exception is a "
         "false positive."),
        ("Warning / Error", "The two grades a rule can give; only Error opens or attaches an "
         "incident."),
    )
    gl = "".join(f"<tr><td><b>{_e(a)}</b></td><td>{_e(b)}</td></tr>" for a, b in glossary)
    return f"""
<h1 class="chapter">Appendix A. Report line and MDRM crosswalk</h1>
<div class="notice">{_e(D.MDRM_DISCLAIMER)}</div>
<table><tr><th>MDRM</th><th>Report</th><th>Schedule</th><th>Item</th><th>Caption</th><th>Rules</th></tr>{xwalk}</table>
<h2>Appendix B. Business calendar</h2>
<p>{_e(D.CALENDAR_NAME)}. Business days are Monday to Friday except these Federal Reserve
holidays. Quarter-end COBs in the second half of 2026: {qe}.</p>
<table><tr><th>Date</th><th>Holiday</th></tr>{hol}</table>
<h1 class="chapter">Appendix C. Change log and retired rules</h1>
<h2>C.1 Re-graded in v3.2</h2><ul>{regraded}</ul>
<h2>C.2 Retired rules</h2>
<p>These rules are no longer in force. Their ids are not reused; Eagle no longer runs them.</p>
<table><tr><th>Rule id</th><th>Title</th><th>Reason</th></tr>{retired}</table>
<h2>Appendix D. Glossary</h2>
<table><tr><th>Term</th><th>Meaning</th></tr>{gl}</table>
"""


def render_html(ds: D.Dataset | None = None) -> str:
    ds = ds or D.generate()
    body = (_cover(ds) + _toc() + _about(ds) + _grading(ds) + _governance(ds) + _systems(ds)
            + _feeds(ds) + _cde_register(ds) + _rule_chapters(ds) + _appendices(ds))
    title = f"{D.RULEBOOK_TITLE} v{D.RULEBOOK_VERSION}"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{_e(title)}'
            f"</title><style>{CSS}</style></head><body>{body}</body></html>")


_FOOTER = ('<div style="font-family: Helvetica, Arial, sans-serif; font-size:7.5pt; color:#5b6573; '
           'width:100%; padding:0 20mm; display:flex; justify-content:space-between;">'
           f"<span>{_e(D.BANK)} &middot; Loan Data Quality Rulebook v{D.RULEBOOK_VERSION} &middot; "
           "Internal &middot; Synthetic</span>"
           '<span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span>'
           "</div>")


def _pin_dates(pdf: bytes) -> bytes:
    """Chromium stamps CreationDate/ModDate; pin the 14 digits (same length, so the xref offsets
    stay valid) so identical input gives identical bytes."""
    return re.sub(rb"D:\d{14}", b"D:20260701000000", pdf)


def render_pdf(out: Path = OUT, html_out: Path | None = None) -> Path:
    from playwright.sync_api import sync_playwright

    page_html = render_html()
    if html_out:
        Path(html_out).write_text(page_html, encoding="utf-8")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(page_html, wait_until="load")
        pdf = page.pdf(format="Letter", print_background=True, display_header_footer=True,
                       header_template="<span></span>", footer_template=_FOOTER,
                       margin={"top": "20mm", "bottom": "20mm", "left": "18mm", "right": "18mm"},
                       prefer_css_page_size=True)
        browser.close()
    out.write_bytes(_pin_dates(pdf))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--html", type=Path, default=None)
    args = ap.parse_args()
    path = render_pdf(args.out, args.html)
    print(f"wrote {path} ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
