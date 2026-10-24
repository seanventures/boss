# Knowledge Graph — product build spec (Graph area, Rules, Learning loop)

Status: build spec, 2026-10-24. Branch base `rel/rc10.10-kg`. Builds on BOSSAPP-622
([KNOWLEDGE_GRAPH_STATUS.md](KNOWLEDGE_GRAPH_STATUS.md)). The operator approved the UX in the mock.
The UX description in §5 is the reference. Where it differs from the mock, §5 wins.

Every claim about today's code cites `file:line` on this branch. Every number about data comes
from the live `boss-community-db-1` database, read-only, on 2026-10-24.

---

## 0. Where we are (research summary)

**Storage.** `kg_nodes`/`kg_edges` are the source of truth (`db/schema.sql:4834`, `:4883`). Each
node already has `trust_tier` (observed/governed/inferred/approved), `created_by`, `weight`,
`valid_from/valid_to`, `repo`, `path` and `attrs`. A generated `knowledge_layer` column
(`db/schema.sql:4979`) puts every kind in one of four layers: `code`, `lifecycle`, `ops`,
`knowledge`. The kind lists are in `engine/boss_engine/services/knowledge/scopes.py:31`. The node
and edge kinds are closed sets (`store.py:29`, `store.py:37`). Edges such as `approved_by`,
`evidenced_by`, `about`, `supersedes`, `caused_by` and `fixed_by` already exist. There are no
`consulted`, `reused` or `proposed_by` edges. The store decides who may write which tier
(`store.py:72`): an agent writes `inferred` only, and only a person sets `approved`
(`store.py:1627`).

**AGE.** There is one AGE graph per project. The vertex label is the layer (`Code`, `Lifecycle`,
`Ops`, `Knowledge`) and the edge label is the kind (`age.py:37`, `age.py:180`). It is a
projection, re-synced after writes (`age.py:363`). Read paths use the recursive CTE
(`store.py:1297`).

**Live data.**

| Project | Live nodes | Live edges | Notes |
|---|---|---|---|
| `northpeak-bank-a` (Fineract) | 51,319 | 234,187 | All code: 43,153 symbols (34,777 methods, 7,074 classes, 873 interfaces, 303 enums, 62 records), 6,508 files, 1,155 modules, 404 endpoints, 96 tables. 36 top-level areas, 1,151 packages (`attrs.module`) |
| `boss-dogfood` | 1,693 code nodes + learning nodes (below) | 3,097 | Learning nodes: 38 `run`, 9 `incident`, 1 `pull_request` |

- **Learning nodes have no edges.** Every edge in the database starts at a code node (query:
  `edges_noncode = 0`).
- **Nothing in the learning stores.** There are 0 `fact` nodes, 0 `kg_usage` rows and 0
  `kg_audit` rows.
- **Embeddings.** There are 9 embeddings, all on incidents
  (`azure:text-embedding-3-small:1536`).

So the code graph is real and large. Learning today is flat text captured when a run or incident
finishes (`ingest.py:40`, `capture.py:60`). It has no root cause, fix or precedent structure, and
nothing links it back to code.

**What agents get today.**

- **Knowledge pack.** Each agent turn gets a budgeted pack (`agent_loop.py:1862` →
  `runtime.py:211`). Its fact ids are logged to `kg_usage` as *shown* (`runtime.py:263`,
  `store.py:1499`). When the run finishes, they are attributed to its outcome (`runtime.py:276`).
- **Citations.** A typed decision may cite `cited_fact_ids` (`typed_decisions.py:41`).
- **Stage scopes.** Scopes limit layers and code reach per stage (`scopes.py:107`).
- **Proposing facts.** Every shipped agent holds `knowledge.read` and `knowledge.propose`
  (`agent_baseline.py:49`). `kg_propose_fact` writes one `inferred` fact (`knowledge_tools.py:129`,
  `service.py:225`).
- **No consulted record.** There is no record of *consulted* that is separate from *shown*, and
  no rule concept.

**API.** `/v1/knowledge/*` has search, node, blast radius, graph status, promote/reject/forget,
Cypher and repositories (`apps/api/app/routers/knowledge.py:1`).

- Search is hybrid, and it calls the embedding model on every query (`service.py:68`). It also
  makes one `get_node` call per hit (`service.py:86`). That is wrong for type-ahead.
- `promote` audits `actor_kind` only, not who approved (`service.py:276`).
- Permissions are `knowledge:read`, which every preset holds (`packages/identity/catalogue.yaml:115`),
  and `knowledge:manage`, held by ceo/cto/executive_console (`services/members.py:236`).

**Console.** The console is Next 16 + React 19, and its only UI dependency is Phosphor icons
(`apps/web/package.json:14`). There is no graph library. The workflow canvas is in-house
(`apps/web/lib/workflow-graph.ts`, tested by `tests/workflow-graph.test.ts`).

- Areas are left-rail entries (`app/ConsoleShell.tsx:201`). Ids live in `lib/area-ids.ts:11`.
  Each area is mounted in `app/page.tsx` behind `shows(...)`, as Observability is
  (`page.tsx:3054`, own folder `app/observability/` with `obs.css`).
- The knowledge screens sit inside **Reliability** (`page.tsx:3155-3167`). Fact promotion sits
  inside **Approvals** (`page.tsx:3675`).
- The design tokens include `--graph: #0c1526` (`app/design-system.css:16`).
- Tests: vitest + happy-dom for units (`apps/web/tests/*.test.ts[x]`). Browser e2e uses the
  harness's console walk (`tests/e2e/harness/console.py`, `console_contract.py:205`).

**Governance building blocks.** Decisions have `approved_by/approved_at` (`db/schema.sql:577`).
The decision action contract lists every action (`services/decision_actions.py:196`). An
entitlement port gates capabilities (`engine/boss_engine/defaults/entitlement.py:6`). The
knowledge budget category is `knowledge` (`service.py:432`). **Projects cannot be archived or
deleted.** The `projects` table has only `id, workspace_id, name, slug, created_at, description`.

**Measured on Fineract (live DB, cold, one run each).**

| Query | Time | Verdict |
|---|---|---|
| Ad-hoc inter-area weighted call edges | 523 ms | Over the 300 ms overview target |
| One class's call neighbourhood, lifted from its 88 methods (1,850 edges, 180 classes) | 892 ms | Over the 200 ms target |
| Building the whole class-level call rollup (20,201 class pairs, 89,650 calls) | 601 ms | Cheap enough to rebuild after every index |

The product therefore reads **precomputed rollups** (§2.4), not live aggregation.

---

## 1. Goals, non-goals, user stories

**Goals**
1. **Learning while executing (the centre of this build, §4).** The Learning layer holds **new**
   knowledge that agents produce **while they execute tasks**. It is not a re-index of what the
   repository already says. It is written in real time inside the one agent pipeline, with
   provenance (agent, run, step, timestamp) and links to the code and rules involved:
   - investigation results: what an RCA tried, ruled out and found;
   - test failures and what fixed them;
   - review findings;
   - decisions and their reasons;
   - build and deploy problems and their workarounds;
   - rules discovered or corrected during a task.

   Later tasks by other agents retrieve these through the pipeline's common context assembly.
   The graph records `consulted`/`reused` edges, so the console can say "learned by X in run A →
   used by Y in run B". A measurable proof is required (§4.6): the later run is cheaper or
   faster because it reused a learning.
2. **Graph area.** A per-project **Graph** area where a person sees the whole repository's shape,
   focuses any symbol, rule or learning item, and follows connections. It must be fast on a
   Fineract-size graph. It updates live while a run writes learnings.
3. **Three governed layers.** Three layers, kept apart: **Code** (automatic index), **Rules**
   (agents propose, people approve) and **Learning** (agents write as they work). **Domain** comes
   later.
4. **Archive a project.**
5. **Customer-grade.** Every screen fully styled, browser-tested by an independent tester, and
   packageable as a paid add-on (§3.7).

**Non-goals (this build).**
- The Domain layer and document ingestion (regulatory, AML, claims) are out. The data model
  reserves them.
- Editing code nodes by hand is out.
- A general Cypher UI is out (the `/cypher` route stays for agents).
- Treating a re-index or a run's finish-time summary as learning is out. Learning is what agents
  record while they work (§4.1).
- Cross-project graphs are out.
- 10M-node scale targets are out (BOSSAPP-622 owns them).
- Changing stage-scope semantics for existing layers is out.

**User stories (acceptance = the demo walk in §6.4).**
- **U1 Whole repo.** As an operator I open Graph and see areas as bubbles, with weighted
  directed call edges between them, plus Rules and Learning bubbles. It fits the window.
- **U2 Focus.** I search "disburse", the best match is selected, and the graph shows it and its
  neighbours within 1–3 hops. Only nodes connected to it are drawn.
- **U3 Tree.** I browse areas → packages → classes → methods in an IDE-style tree. Clicking a
  method focuses it.
- **U4 Detail.** Clicking any node shows its layer, type, statement, attributes, clickable
  connections, vector info and provenance.
- **U5 Scout proposes rules.** Agent X (code analyst, "Scout") runs *Scan for business rules* on
  a module within a budget. Its proposed rules arrive in Rules review with evidence edges to the
  code.
- **U6 Approve.** I Approve, Edit or Reject each rule. An approved rule is trusted input for
  every agent from then on.
- **U7 Builder consults.** Agent Y (engineer, "Builder") changes code in that module. Its run
  records which rules it consulted (`consulted` edges).
- **U8 Responder learns.** Agent Z (SRE, "Responder") triages an incident using rules, the code
  graph and precedents. *During* the investigation it records what it tried, ruled out and found.
  At the end it writes root cause, fix, a new rule proposal and a precedent. A later incident run
  **reuses** the precedent (`reused` edge) and resolves in fewer steps (§4.6).
- **U8b Builder learns from a failing test.** While Builder's build is in a repair round, the
  failing test and the change that made it pass are recorded as a `test_failure` → `fix` pair on
  the symbols involved. A later build touching those symbols gets it in context and consults it.
- **U8c Live.** I watch a run in progress. Its learnings appear in the run view's "Learned so far"
  strip and in the Graph within seconds, without reloading the page.
- **U9 Archive.** I archive a stale project. It leaves the project picker, nothing new runs on
  it, and I can restore it.

---

## 2. Data model

### 2.1 Two layer notions, both explicit

| Notion | Values | Where | Used by |
|---|---|---|---|
| **Storage layer** (existing) | `code`, `lifecycle`, `ops`, `knowledge`, **`rules`** (new) | `kg_nodes.knowledge_layer` generated column; AGE vertex label | stage scopes, AGE scope checks |
| **Product layer** (new) | `code`, `rules`, `learning`, `domain` | `scopes.product_layer(kind)` + `product_layer_sql_case()`; AGE vertex property `product_layer`; every API node payload `layer` | console chips, API filters, permissions |

- **Why `rules` becomes a storage layer.** A stage scope must be able to grant or refuse rules
  on their own. Example: triage reads approved rules but not code. Add `"rules": {"rule"}` to
  `LAYER_KINDS` (`scopes.py:31`) and `"rules": "Rules"` to `LABELS` (`age.py:37`).
- **Rebuilding the generated column.** The CASE changes, so the column is rebuilt with a DROP +
  ADD of the generated column. That rewrites `kg_nodes`. On 5 × 51k rows this is seconds. The
  upgrade notes say so. `test_knowledge_stage_scopes_db` keeps SQL and Python in step, as today.
- **Product-layer mapping** (one function, one test):
  - `code` = storage `code` plus the infra topology kinds (`service`, `resource`, `deployment`,
    `datastore`, `external`, `repo`).
  - `rules` = `rule`.
  - `learning` has two parts:
    - **learned items**, written by agents as they work (§4): `investigation`, `test_failure`,
      `fix`, `workaround`, `review_finding`, `decision`, `root_cause`, `precedent`, `fact`;
    - **anchors** they hang on: `run`, `incident`, `pull_request`, `release`, `spec`, `runbook`,
      `alert_rule`, `topic`.

    Anchors are drawn smaller-labelled and are never counted as "learned". The finish-time
    summary capture (`ingest.py:40`) only creates or refreshes the `run`/`incident` anchor. It is
    not learning.
  - `domain` = reserved: `regulation`, `policy_clause`, `document`. Not written in this build.
- **Stage defaults gain `rules`.** Every stage in `DEFAULTS` (`scopes.py:107`) gets `rules`,
  because approved rules are trusted input for every agent. `TOOL_LAYERS` (`scopes.py:138`) gains
  `kg_rules: {"rules"}` and `kg_propose_rule: {"rules"}`.

### 2.2 New kinds

Add these to `NODE_KINDS` (`store.py:29`): `rule`, `fix`, `precedent`, `investigation`,
`test_failure`, `workaround`. `incident`, `root_cause`, `decision`, `review_finding` and `run`
already exist.

Add these to `EDGE_KINDS` (`store.py:37`):

| Edge | From → To | Meaning |
|---|---|---|
| `consulted` | run → rule / precedent / fact / root_cause | the run's agent was given it **and** used it (attrs.how = `pack` · `tool` · `cited`; attrs.step) |
| `reused` | run → precedent / fix | a later run applied a precedent (cited in its output) |
| `proposed_by` | rule / fact → agent | which agent proposed it (attrs.run_id, step) |
| `produced` | run → any learned item or rule | what a run wrote, with attrs `step` and `at` |
| `ruled_out` | investigation → symbol / root_cause | a hypothesis the investigation tested and rejected (attrs.reason) |
| `corrects` | rule (new version) → rule | a task found an approved rule wrong or incomplete; a person approves the correction |
| `governs` | rule → symbol / package / area node | where an approved rule applies (from its scope) |

These existing edges are reused: `evidenced_by` (rule → symbol, with attrs.lines, attrs.excerpt),
`approved_by` (rule → person), `supersedes` (edited rule → its previous version), `caused_by`
(incident → root_cause), `fixed_by` (root_cause → fix; fix → pull_request), and `about`.

### 2.3 Node schemas (in `attrs`, validated in `services/knowledge/rules.py` and `learning_writeback.py`)

**Rule** (`kind='rule'`, `node_key='rule:<sha1(scope+statement)[:16]>'`, `name`=title,
`body`=statement):

```
attrs: {
  statement, rationale, category: validation|calculation|limit|state_transition|authorization|other,
  scope: {repo, areas[], packages[], symbols[]},          # what it governs
  status: proposed|approved|rejected|superseded,
  confidence: 0..1,
  provenance: {agent_role, agent_name, run_id, workflow_run_id, step, model_id, proposed_at},
  review: {by, at, decision_id, note} | null,            # set on approve/edit/reject
  version: 1..n, previous_id | null
}
trust_tier: inferred (proposed) → approved (person, store-enforced). Rejected rules are closed (valid_to).
```

**Learned items** (one envelope; validated in `learning_writeback.py`). `name` is a one-line
title, and `body` is the scrubbed statement (`scrub.py`). `trust_tier` is `inferred` and the tier
is store-enforced.

```
attrs: {
  learning_kind: investigation|test_failure|fix|workaround|review_finding|decision|root_cause|precedent|fact,
  statement, detail: {...kind-specific...},
  about: {symbols[], rules[], areas[]},                     # resolved to `about` edges
  evidence: [{type: log|test|diff|metric|file, ref, excerpt}],
  confidence: 0..1, dedupe_key, seen_count, last_seen_run,
  provenance: {agent_role, agent_name, run_id, workflow_run_id, code_task_run_id, step, model_id, at}
}
```

The kind-specific `detail` fields:

| `learning_kind` | `detail` fields |
|---|---|
| `investigation` | `tried[]`, `ruled_out[{hypothesis, reason}]`, `found` |
| `test_failure` | `test`, `error_signature`, `cause` |
| `fix` | `change_summary`, `files[]`, `pr_url?` |
| `workaround` | `problem` (build/deploy), `workaround`, `permanent_fix?` |
| `decision` | `question`, `chosen`, `reasons[]`, `alternatives[]` |
| `precedent` | `symptom`, `cause_summary`, `fix_summary`, `applies_to`, `reuse_count` |

**Provenance on every product write.** The store gains `actor_ref` (who: a person id or an agent
role key) next to `actor_kind`:

- `kg_nodes.created_by` is set to the agent role key or person id, not `'system'`.
- `kg_audit` gains `actor_ref TEXT`. `service.promote/reject` (`service.py:266`) record it.

### 2.4 View rollups (new, derived, rebuildable)

```sql
-- db/schema.sql, block "BOSSAPP-KGV view rollups"
CREATE TABLE IF NOT EXISTS kg_view_tree (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  key TEXT NOT NULL,              -- 'area:<repo>:<area>' | 'pkg:<repo>:<pkg>' | class/method node_key
  parent_key TEXT,                -- NULL for areas
  level TEXT NOT NULL CHECK (level IN ('area','package','class','method')),
  node_id TEXT,                   -- kg_nodes.id for class/method, NULL for area/package
  label TEXT NOT NULL,
  symbol_kind TEXT,               -- class|interface|enum|record|method|function
  child_count INTEGER NOT NULL DEFAULT 0,
  class_count INTEGER NOT NULL DEFAULT 0,
  sort_key TEXT NOT NULL,
  PRIMARY KEY (project_id, key)
);
CREATE INDEX IF NOT EXISTS idx_kg_view_tree_parent ON kg_view_tree(project_id, parent_key, sort_key);
CREATE INDEX IF NOT EXISTS idx_kg_view_tree_node ON kg_view_tree(project_id, node_id);
CREATE TABLE IF NOT EXISTS kg_view_edges (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  level TEXT NOT NULL CHECK (level IN ('area','package','class')),
  src_key TEXT NOT NULL, dst_key TEXT NOT NULL,
  kind TEXT NOT NULL,             -- calls (calls+dispatches_to) | imports | extends (extends+implements)
  weight INTEGER NOT NULL,
  PRIMARY KEY (project_id, level, src_key, dst_key, kind)
);
CREATE INDEX IF NOT EXISTS idx_kg_view_edges_dst ON kg_view_edges(project_id, level, dst_key);
CREATE TABLE IF NOT EXISTS kg_view_sync (
  project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  built_at TIMESTAMPTZ NOT NULL, nodes INTEGER NOT NULL, edges INTEGER NOT NULL, ms INTEGER NOT NULL
);
```

**Area rule.** The area is the first path segment of the file (the Gradle/Maven module, e.g.
`fineract-provider`). If one segment holds more than 80% of the symbols, the area is the first
segment *below* the longest common prefix (e.g. `src/`). This keeps single-module repos useful.
The package is `attrs.module`, falling back to the directory. A method's owning class is its
node_key minus the last `.member`. Example: `…Impl.java:LoanWritePlatformServiceJpaRepositoryImpl.disburseLoan`
belongs to `…:LoanWritePlatformServiceJpaRepositoryImpl`.

**Build.** `services/knowledge/view_rollup.py rebuild(connection, project_id)` deletes and
re-inserts the project's rollup rows in one transaction. The class-level edge insert is the
measured query:

```sql
WITH own AS (
  SELECT id,
         CASE WHEN attrs->>'symbol_kind' = 'method' THEN regexp_replace(node_key, '\.[^.:]*$', '') ELSE node_key END AS ck,
         coalesce(attrs->>'module', regexp_replace(path, '/[^/]*$', '')) AS pkg,
         :area_expr AS area
  FROM kg_nodes WHERE project_id = :p AND kind = 'symbol' AND valid_to IS NULL)
INSERT INTO kg_view_edges (project_id, level, src_key, dst_key, kind, weight)
SELECT :p, 'class', s.ck, d.ck, 'calls', count(*)
FROM kg_edges e JOIN own s ON s.id = e.src_id JOIN own d ON d.id = e.dst_id
WHERE e.project_id = :p AND e.valid_to IS NULL AND e.kind IN ('calls','dispatches_to') AND s.ck <> d.ck
GROUP BY s.ck, d.ck;
-- package and area levels: the same join grouped by s.pkg/d.pkg and s.area/d.area
-- (the area level measured 523 ms ad hoc; inside the rebuild it reuses `own`).
```

**When it runs.**
- At the end of a successful repository index job (`repo_index.py:549 _finish`, state `indexed`).
- From the worker's 10-minute reconcile (`ingest.py:166`) when `kg_view_sync.nodes` differs from
  the live code count.
- Measured cost on Fineract is about 0.6 s for classes, so the target for all levels is under 3 s.

Rules and Learning are **not** rolled up. They are small and read live.

### 2.5 Durable "consulted" and "reused"

**`kg_usage` gains columns** (`db/schema.sql:4998`):

| Column | Values / meaning |
|---|---|
| `use_kind` | `shown` · `consulted` · `cited` · `reused`; default `shown` |
| `workflow_run_id`, `code_task_run_id` | from the LLM context, when known |
| `agent_role` | the role whose turn used it |

`run_id` stays as the agent run id.

**How edges are derived.** When ingest captures a run node (`ingest.py:75`), it derives the
`consulted` and `reused` edges from `kg_usage` for that run, as `capture.sync_change_links` does
for `changed` (`capture.py:166`). This means usage written *before* the run node exists is never
lost. Re-ingest is idempotent: the edge unique key is `(project, src, dst, kind)`.

**What each `use_kind` means.**

| `use_kind` | Recorded when |
|---|---|
| `shown` | the item was in the injected pack. This is today's behaviour. It does **not** draw an edge |
| `consulted` | a `kg_rules` / `kg_precedent` / `kg_why` tool call returned it, or the stage's "rules in scope" section included it and the step's output cited its id |
| `cited` | the output's `cited_fact_ids` / `cited_rule_ids` named it |
| `reused` | a cited `precedent` or `fix` whose `applies_to` matches the run's incident or area |

### 2.6 AGE projection

- **Vertices.** The `Rules` label is new. Every vertex gets the `product_layer` property; add it
  to `VERTEX_EXTRAS` (`age.py`). Rule vertices also get `status`.
- **Edges.** New kinds become edge labels automatically (`age.py:98 _ensure_label`).
- **Rebuild.** A full rebuild runs once after the migration via `reconcile`.
- **Cypher scope.** `check_scope` (`age.py:441`) refuses the `Rules` label to a stage without
  `rules`.

### 2.7 Project archive

```sql
ALTER TABLE projects ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS archived_by TEXT;
```

**What archived means.**
- The project is hidden from `GET /v1/projects` unless `include_archived=true`.
- It refuses new workflow runs, code tasks, schedules, index jobs and ingest. Each refusal is
  `409 project archived`, in words.
- Reads keep working, and nothing is deleted.
- Workers skip archived projects in `repo_index.enqueue_due`, `ingest.tick`,
  `workflow_schedules` and the monitoring scheduler. A guard helper `projects.is_archived()` is
  used everywhere, with one enforcing test that greps the scheduler entry points.

---

## 3. API

There is one new router, `apps/api/app/routers/knowledge_view.py`, with prefix
`/v1/knowledge/view`. Every route takes `project_id` and `actor`, and uses
`@permit(P.KNOWLEDGE_READ, scope=Scope.project(...))` plus `require_project_actor`, as
`knowledge.py:43` does. Payload nodes always have this shape:
`{id, key, kind, symbol_kind?, layer, label, sub?, trust_tier, status?, counts?}`.

| Route | Purpose | Notes |
|---|---|---|
| `GET /overview?layers=code,rules,learning&repo=` | Whole repo: area bubbles + Rules + Learning bubbles + weighted edges | Reads `kg_view_tree` (level `area`) and `kg_view_edges` (level `area`, kind `calls`). The Rules bubble is a count of rules by status. The Learning bubble is a count by kind. Layer edges: Rules → areas with `governs`/`evidenced_by` counts; Learning → areas with `about`/`changed` counts. Returns `{areas[], layers[], edges[{src,dst,kind,weight,label}], built_at, stale}`. `label` is pre-formatted ("16,749 calls") |
| `GET /neighbourhood?node=&depth=1..3&layers=&limit=80` | Focus | See below |
| `GET /node/{id}` | Right panel | Node + attrs + provenance + connections grouped by edge kind (first 20 per kind + total) + vector info `{model_id, dimensions, neighbours[5]}` (nearest by `vector_search`, `store.py:1129`, only if the node has an embedding; otherwise `{embedded:false, reason}`) + usage counts (consulted/reused by N runs) |
| `GET /tree/children?parent=&tab=project\|rules\|learning&q=` | Lazy tree | `project`: `kg_view_tree` children of `parent` (NULL = areas), sorted, paged 200. `rules`: grouped by status → category. `learning`: grouped by kind → newest first. With `q`, returns the matching rows **plus their ancestors** so the tree opens to them |
| `GET /search?q=&layers=&limit=20` | Type-ahead | Name and `search_tsv` only (`idx_kg_nodes_name`, GIN). **No embedding call** and **no per-hit `get_node`** (one query). Ranking: exact name > prefix of last segment > tsv rank; classes before methods at equal rank. `meaning=1` opts into the existing hybrid search (`service.py:31`) and is labelled "by meaning" |
| `GET /rules?status=proposed&scan_id=` | Rules review queue | With evidence (symbol label, path, lines) |
| `POST /rules/{id}/approve` · `/edit` · `/reject` | Review actions | Body `{project_id, actor, step_up:"confirmed", note?, statement?, scope?}`. **Edit** creates version n+1 (`supersedes` the old one), approved in the same act. **Approve** sets `trust_tier='approved'` via `set_trust` (`store.py:1627`), adds an `approved_by` edge to the person, writes `attrs.review` and `kg_audit` with `actor_ref`, and derives `governs` edges from scope. **Reject** closes the node with `review.note` |
| `POST /v1/projects/{id}/archive` · `/unarchive` | Archive | `project:delete` permission, step-up confirmed. Returns the project with `archived_at`. Lives in `apps/api/app/routers/projects_archive.py` |

**Neighbourhood algorithm.**
- For a code class or package node, it walks `kg_view_edges` at the node's level, both
  directions, BFS to `depth`. At each hop it keeps the top `limit/depth` neighbours by weight.
- For a method, it walks `kg_edges` (`calls`, `dispatches_to`, `overrides`), plus the owning
  class as context.
- For rule and learning nodes, it walks `kg_edges` of any kind.
- Layer filter: an edge to a node outside the `layers` chips is dropped.
- Returns `{center, nodes[], edges[{src,dst,kind,weight,label}], truncated, hidden:{nodes,edges}}`.
  The UI says "Showing 80 of 180 connected classes" when truncated.

**Permissions per layer.**
- **Code** and **Rules** read: `knowledge:read`.
- **Learning** read: `knowledge:read`. Nodes of kind `decision`/`precedent` are also filtered by
  `decision:read`, as `kg_precedent` requires (`knowledge_tools.py:198`).
- **Rule review**: new permission `knowledge:review` in `packages/identity/catalogue.yaml`
  (regenerated by `identity/codegen`). It is granted to ceo/cto/executive_console and to a new
  optional "Domain reviewer" preset, so a business expert can approve rules without
  `knowledge:manage`.
- **Forget/promote facts**: stays `knowledge:manage`.

**Performance targets** (p95, warm, `northpeak-bank-a` 51k/234k; measured by
`engine/tests/test_knowledge_view_perf_db.py` against a fixture loaded from a dump of that
project, opt-in with `BOSS_KG_PERF=1`):

| Route | Target | Expected |
|---|---|---|
| overview | < 300 ms | Reads at most 36 areas + ~1.3k edge rows |
| neighbourhood, depth ≤ 3, limit 80 | < 200 ms | |
| node detail | < 150 ms | |
| tree children | < 100 ms | |
| search | < 150 ms | |
| rollup rebuild | < 3 s | |

A route over target fails the perf test. It is never waived.

### 3.7 Packaging (add-on)

Every Rules/Learning write route and the scan capability call
`EntitlementPort.check(project_id, "knowledge.rules")` (`defaults/entitlement.py:12`). Community
is allow-all, as today. A commercial port can gate it. Code-layer views stay ungated. The console
reads the entitlement from `/v1/knowledge/graph-status` and shows a styled locked state
(`components/locked-panel.tsx`), never a broken button.

---

## 4. Learning while executing, and the agent capabilities

All of these go to every agent through the existing baseline bundles (`agent_baseline.py:49`).
There are no role branches ("every agent knows BOSS fundamentals"). Scout, Builder and Responder
differ only by the workflow step they run.

### 4.1 Principle

Learning is written **at the moment the agent learns it**, inside the one agent pipeline
([ONE_AGENT_PIPELINE.md](ONE_AGENT_PIPELINE.md)). It is never reconstructed afterwards from the
repository or a run summary.

There are two write paths. They share one service, `learning_writeback.record()`, so dedupe,
scrub, provenance and live events behave the same for both:

1. **The agent says it.** The tool `record_learning` is in the `knowledge.propose` bundle, which
   every agent holds. An agent calls it whenever it rules something out, finds a cause, fixes a
   failure, works around a build/deploy problem or makes a decision with reasons.
   - Args: `learning_kind`, `title`, `statement`, `detail`, `about{symbols,rules,areas}`,
     `evidence[]` (at least one), `confidence`.
   - It is a `write` tool with reversibility `safe`. What it writes is `inferred`, quarantined and
     forgettable, like `kg_propose_fact` (`knowledge_tools.py:178`).
   - Provenance is taken from the LLM context (`llm_telemetry.get_llm_context`: role, run, stage),
     **never from the arguments**.
   - The pipeline primer (`boss.fundamentals`) tells every agent when to call it. Each "when"
     becomes a battle-test scenario, not a keyword rule.
2. **The pipeline sees it.** Stage-end hooks record what the pipeline itself observes, with no
   extra model call:

| Hook (where) | Writes |
|---|---|
| Build stage boundary, `governed_code_task_service.py:4445` (`_abort_if_cancelled`, called at every stage) and the repair checkpoint `:6103` (`implement-repair{n}`) | `test_failure` (failing test + error signature from the gate output) when a repair round starts; `fix` linked `fixed_by` when the next gate passes, `about` the files/symbols the repair changed (`runtime.remember_written_files`, `runtime.py:117`) |
| Engine turn stream, `harness_failures.check_turn` (`harness_failures.py:300`, every engine's turns) | `workaround` when a build/deploy command failed and a later command in the same stage succeeded with a changed approach (the pair is recorded; classification of *harness* failures stays where it is) |
| Workflow step completion, `workflows.complete_step` (`workflows.py:6022`) | the step's typed outputs: `investigation` (tried / ruled out / found) from investigate steps, `review_finding` from review steps, `decision` (question, chosen, reasons) from typed decisions, `root_cause`/`fix`/`precedent` from `incident_report` |
| Domain-agent decisions, `AgentService.decide` (ONE_AGENT_PIPELINE.md "A structured decision") | `decision` with the chosen action's reasons and the read results it relied on |

A step's typed output schema gains an optional `learned[]` array with the same shape as
`record_learning`'s arguments. An engine that cannot call tools still contributes, which keeps
the behaviour model-agnostic.

### 4.2 Context assembly (retrieval into later tasks)

- **Where.** `service.pack` (`service.py:349`) is called by the turn entry
  (`agent_loop.py:1862` → `runtime.quoted_pack_for_turn`, `runtime.py:211`) and by every stage.
  It gains two sections, each inside the stage budget:
  - **Rules in scope**: approved rules whose `governs` edges reach the seed symbols, or their
    class, package or area. Quoted `[R:<id>]`.
  - **Learned before**: learned items `about` the seed symbols or rules, ranked by similarity to
    the task, `weight` (`learning.py`) and recency. Quoted `[L:<id>]`, with "learned by
    <agent> in <run>, <date>".
- **What is never injected.** Proposed (unapproved) rules. They are visible only via
  `kg_rules(status='proposed')`, labelled untrusted.
- **Citations.** The typed output schema gains `cited_rule_ids` and `cited_learning_ids` beside
  `cited_fact_ids` (`typed_decisions.py:95`).
- **Tools.**
  - `kg_rules` (in `knowledge.read`) returns approved rules for symbols, packages, areas or a
    query.
  - `kg_precedent` (`knowledge_tools.py:150`) returns learned items first for incident stages.
- **Usage records.** Tool hits, injected items and citations are written to `kg_usage` with
  `use_kind` per §2.5. `consulted`/`reused` edges follow from them, so the graph can say
  "learned by X in run A → used by Y in run B".
- **Outcome weighting.** Usage also feeds outcome attribution (`runtime.py:276`), so a learning
  that keeps being followed by failure decays (`learning.py:23`) and drops out of packs.

### 4.3 Quality control

| Control | How |
|---|---|
| **Dedupe** | `dedupe_key` = hash(kind, normalised error signature or statement, sorted `about` keys). A repeat increments `seen_count`, appends evidence and `last_seen_run`, and adds a `produced` edge from the new run. It does not create a second node |
| **Contradiction** | A new learning that negates an open one on the same `about` (archivist's same-predicate check, `archivist.py:150`) closes the old one (`supersedes`). Both stay visible in history |
| **Confidence** | The agent's confidence is capped by evidence. No evidence means refused, a log excerpt alone caps at 0.6, and a passing test caps at 0.9. Items below 0.4 are stored but not injected |
| **Scrub** | `scrub.py` runs on every body. A secret shape refuses the write (`capture.py:75`) |
| **Human approval** | A learning never becomes a rule by itself. Agents propose rules and corrections (`kg_propose_rule`, `corrects` edge). Only a person approves them in Rules review. Learnings are inferred knowledge, labelled as such, and forgettable by a person (`knowledge:manage`) |
| **Budget** | Hook writes are free (no model). `record_learning` is a tool call inside the turn's existing budget. Embedding a learning goes through the configured embedder under budget category `knowledge`, and is skipped with a stated reason when refused |

### 4.4 Live in the console

- **Event.** `record()` emits `knowledge.learning_recorded` through `emit_event`
  (`core.py:63`) with `{node_id, learning_kind, run_id, step}`.
- **Feed.** `GET /v1/knowledge/view/live?since=<cursor>&run=` returns learnings, rules and new
  `consulted`/`reused` edges since the cursor.
- **Polling.** The Graph area polls it every 5 s, only while visible and only while a run is
  active, following "an area off screen polls nothing" (`apps/web/tests/an-area-off-screen-polls-nothing.test.tsx`).
- **Run view.** The build cockpit (`app/factory/cockpit.tsx`) and the workflow run view get a **Learned so far** strip. It
  lists the run's learnings and its consulted items as they arrive. Each one is clickable and
  opens the Graph focused on it.
- **Graph.** New nodes fade in at their place in the Focus view. They are not re-laid out from
  scratch.

### 4.5 Rules capabilities

1. **`kg_propose_rule`** (in the `knowledge.propose` bundle, a write tool, reversibility `safe`).
   - Args: `statement`, `title`, `category`, `scope{areas,packages,symbols}`,
     `evidence[{symbol, lines, excerpt}]` (at least one), `rationale`, `confidence`,
     `corrects?` (the id of the approved rule it corrects).
   - It validates that every evidence symbol resolves in the project (`store.find_by_key` or a
     name lookup). Otherwise it refuses in words.
   - It writes an `inferred` rule, plus `evidenced_by`, `proposed_by` and `produced` edges (and
     `corrects`, when given).
   - It deduplicates by node_key.
   - Implemented in `services/knowledge/rules.py propose_rule()`.
2. **Code scan: "Scan for business rules"** (a capability and a catalogue workflow).
   - **Capability** `scan_business_rules` in `workflow_capabilities.py` (`CAPABILITIES`, `:117`).
     - Inputs: `scope` (area/package keys from the tree), `max_files` (default 60),
       `max_rules` (default 25), `budget_usd` (required; default 2.00; at most the project's cap).
     - It reserves budget through the existing governor (category `knowledge`) before any call.
     - It walks the scoped classes in weight order (rollup), reads code with `repo.read`, and
       proposes rules with `kg_propose_rule`.
     - It stops at whichever limit comes first and reports `{files_read, rules_proposed, spend,
       stopped_by}`.
   - **Catalogue entry** `business_rules_scan` in `workflow_catalogue.py`. Steps: `scan` →
     `review`, a workflow gate with action `review_proposed_rules`, added to
     `WORKFLOW_GATE_ACTIONS` (`decision_actions.py:131`).
     - The card links to Graph › Rules review filtered by `scan_id`.
     - It resolves when that scan's queue is empty or a person resolves it.
   - It can be started from the Graph area (an area or package in the tree, or the right panel)
     or as a workflow.

### 4.6 Measurable proof (the release claim)

Proof uses paired runs on the same task class, both on one engine, scripted where possible:

- **Arm A (`knowledge_graph=on`).** The second task runs after the first task's learnings exist.
- **Arm A′ (`keyword`).** Learned items excluded, as today (`scopes.py:208`).

**Pairs.**
1. Incident → second incident with the same symptom.
2. A build whose first attempt needed a repair round → a later build touching the same symbols.

**Metrics** (from run scorecards, [RUN_SCORECARDS.md](RUN_SCORECARDS.md), and `llm_calls`):
agent steps/turns, tokens, model cost, wall time to resolution, and repair rounds.

**Claim rule.** The claim is made only if A beats A′ on the median of at least 3 pairs per task
class, **and** A's run carries the `reused`/`consulted` edge to the earlier learning. Without the
edge, the improvement is not attributed to learning. Results go to
`docs/product/KNOWLEDGE_GRAPH_STATUS.md` with run ids, as the case study protocol asks
([KNOWLEDGE_GRAPH_CASE_STUDY.md](KNOWLEDGE_GRAPH_CASE_STUDY.md) §4–5). A result that does not
beat A′ is reported as such.

Test: `engine/tests/test_learning_reuse_proof_db.py` replays scripted pairs with the conformance
engine. It asserts that the edge exists and that the scripted step count drops. This is plumbing
proof only, not the release claim.

### 4.7 Creating the case study through BOSS's own agents

The case-study projects, workflows and runs (the Fineract bank, the incident pair, the build
pair) are created **by talking to BOSS's agents**, through the console's Ask BOSS or Metatron.
Both are thin clients of `agent_turn` (ONE_AGENT_PIPELINE.md, "Entry points"). They are not
created by scripts or SQL.

**When something is missing.** If an agent cannot do a step, for example it cannot start a scan,
cannot find a learning or cannot explain a consulted edge:
- the gap is a **Plane ticket**;
- it is fixed in the **common path** (the action catalogue, a tool, the primer, or context
  assembly), never in a client;
- it gets a battle-test scenario that runs regularly.

**Tally.** Keep a tally of what was found and fixed, and by whom: BOSS agent vs Claude.

**Retry rule.** No operator retry is used as a test. Prove the fix on the real run first.

---

## 5. Console UI — the Graph area

### 5.1 Placement

- **New rail area** `knowledge`, labelled **Graph**, sub-label "Code, rules and what agents
  learned". It sits in group **Run**, after Agent Studio.
  - Add it to `lib/area-ids.ts`, to `AREAS` in `app/ConsoleShell.tsx:201`, and to a `shows("knowledge")`
    section in `app/page.tsx` that mounts `<GraphArea>`.
  - The area is scoped to the active project, like every area.
- **Mock vs real nav.** The mock's top-nav "Overview · Workflows · Agents · Graph" is not how the
  console navigates. The rail entry is the faithful equivalent. Inside the area, a tab row shows
  **Graph · Rules review · Settings**.
- **What moves.**
  - Reliability keeps its service-topology panel (`KnowledgeGraphPanel`).
  - `KnowledgeExplorer` and `KnowledgeSettings` (`page.tsx:3166-3167`) move to Graph and Graph ›
    Settings.
  - Approvals keeps a one-line link card in place of `KnowledgePromotionQueue`
    (`page.tsx:3675`). Fact promotion moves to Rules review › Facts.
- **Parity table.** It is required before merge (rule "a redesign keeps every capability"):
  search, why, blast radius, index now, repository status, embedding status, retention,
  promote/reject/forget.
- **Legacy links.** `legacy-areas.ts` maps old anchors.

### 5.2 Layout (desktop ≥ 1280 px)

`[Project tree 300px | Graph canvas flex | Detail panel 360px]`, with a toolbar above the canvas.

- Below 1100 px the detail panel becomes a drawer.
- Below 760 px the tree becomes a drawer, so the canvas keeps the width. No horizontal page
  scroll.

**Toolbar.**
- Search box (⌘K focuses it).
- Mode toggle **Whole repo · Focus**.
- Hops stepper 1–3 (Focus only).
- Layer chips **All · Code · Rules · Learning**, multi-select, where All clears the others.
- Zoom **− / + / Fit**.
- Repo picker, if more than one repo.
- Right side: "Built 4 min ago · Rebuild". The Rebuild button calls index now.

### 5.3 Graph canvas (`app/knowledge/graph-canvas.tsx`, layout in `lib/kg-layout.ts`)

- **Rendering.** `<canvas>` 2D, devicePixelRatio-aware, with an offscreen hit-test by node
  index. Hidden nodes and labels outside the viewport are culled.
- **Ceilings.** Overview is ≤ 60 nodes and Focus is ≤ 300 nodes, so canvas holds 60 fps without
  WebGL.
- **Layout** is in-house, deterministic and pure. It is unit-tested like `lib/workflow-graph.ts`.
  - Whole repo: a seeded force layout (fixed iterations, seeded by key so the same data gives the
    same picture), with Rules and Learning bubbles pinned right.
  - Focus: radial by hop, with the centre fixed and neighbours ordered by weight around the ring.
- **No graph dependency.** react-force-graph-2d was considered and not adopted. It pulls d3,
  force-graph and kapsule. It owns zoom and interaction, which conflicts with the styling rules
  below. Its React 19 compatibility was not verified. If layout quality needs it, `d3-force`
  (ISC) is the only permitted addition. Bundle sizes were not measured.
- **Fit to window.** On load, on data change and on resize, it fits the bounding box with 48 px
  padding. Wheel and pinch zoom to the cursor (0.2×–4×). Drag pans. Drag on a node moves it,
  session-only. Double-click focuses the node.
- **Keyboard.** Tab/Shift-Tab cycle nodes in weight order. Enter focuses. Esc returns to the
  previous view. `+`, `-` and `0` zoom and fit.

**Styling (operator decisions, binding).**
- **Shape and size.** All nodes are the **same size**: a 28 px diameter circle, with area and
  layer bubbles at 56 px. Size never encodes count or kind.
- **Fill.** Fills are **solid** colour only. There are no transparent or outline-only fills,
  including interfaces and area bubbles.
- **Selected node.** A 3 px `--ink` ring outside the fill, plus a soft glow.
- **Kind colours** (CSS custom properties in `app/knowledge/kg.css`, read by the canvas):
  - interface `#7dd3fc`
  - class `#5b9dff`
  - method `#94a3c8`
  - rule `#f0a43a`
  - incident `#ff7a6b`
  - root cause `#c4a1ff`
  - fix `#4ade80`
  - precedent `#e879f9`
  - Whole-repo bubbles: code area blue `#5b9dff`, Rules bubble amber `#f0a43a`, Learning bubble
    purple `#c4a1ff`.
  - *Proposed, operator to confirm:* enum/record `#38bdf8`, function `#94a3c8`, endpoint `#2ec5c5`
    (`--teal`), table `#facc15`, investigation `#f9a8d4`, test failure `#fb923c`, workaround
    `#a3e635`, review finding `#fcd34d`, decision `#fda4af`, run/PR anchors `#a5b4fc`, fact
    `#d8b4fe`.
- **Legend.** Lists every kind colour present in the view, collapsible, bottom-left.
- **Edges.** Thin and uniform: 1.5 px, rising to at most 3 px for the heaviest weight on a log
  scale. Weight is shown **mainly by an edge label** such as "3,000 calls". Labels show on
  Whole-repo edges and on Focus edges with weight > 1, and on hover always.
- **Edge colour.** `--muted` at 70%. Edges of the selected node use `--ink-soft`. Edges of
  `consulted`/`reused`/`approved_by` use the target's layer colour, so the learning loop reads
  as colour.
- **Arrowheads.** Clearly visible: about 15 px long, 10 px wide, filled, ending at the node rim.
  Bidirectional pairs are drawn as two slightly curved edges.
- **Text.** Node labels 14 px, area labels 16 px, edge labels 12 px, tree rows 14 px. All use the
  console font and `--ink`/`--ink-soft`.
  - Labels sit under the node on a `--graph` halo, so they stay legible over edges.
  - The label under a bubble includes its counts ("fineract-loan · 869 classes").
  - Long names are middle-ellipsised to 28 characters, with the full name in the tooltip.
- **Canvas background** is `--graph` (`design-system.css:16`). Everything else uses console
  tokens. There are no literal colours outside `kg.css`.

### 5.4 Modes

- **Whole repo.** Area bubbles, inter-area call edges with weight labels, and Rules and Learning
  bubbles with their edges to areas.
  - Clicking a bubble selects it (detail panel).
  - Double-click on an area drills into a package-level overview of that area (breadcrumb
    "Repo › fineract-loan"). This uses rollup level `package`.
  - Double-click on Rules or Learning switches the tree tab and lists that layer.
- **Focus.** The selected node plus neighbours within N hops (1–3), drawn only if connected. The
  hop rings are faint. A "Showing 80 of 180 · Show more" control raises the limit to 300.
  Changing the chips re-queries.
- **Search.** Debounced at 150 ms. A dropdown shows the top 8, with kind dots and path. Enter or
  a pick selects the best match, switches to Focus, centres it, expands the tree to it and opens
  the detail panel.
  - With no match: "No symbol, rule or learning matches 'x'".
  - "Search by meaning" is shown as a link only when an embedding model is configured.

### 5.5 Project tree (`app/knowledge/project-tree.tsx`)

- **Tabs.** **Project · Rules · Learning**.
  - Project: areas → packages → classes (with kind dot and member count) → methods. Children load
    lazily from `/tree/children`.
  - Rules: Proposed (n) / Approved (n) / Rejected (n) → category → rule.
  - Learning: Incidents · Root causes · Fixes · Precedents · Runs → newest first.
- **Rows.** 14 px, 28 px tall. The list is virtualised, because a package may hold 400 classes.
  A row shows a chevron, a solid kind dot, the label and a right-aligned count.
- **Behaviour.**
  - The search filters the tree: matching rows plus their ancestors, with matches highlighted.
  - Clicking a row focuses the node. The selection is kept in sync with the canvas in both
    directions, and the tree auto-scrolls to it.
- **Keyboard.** WAI-ARIA tree pattern (`role="tree"`, `treeitem`, `aria-expanded`). Arrow keys
  move and expand. Enter focuses.

### 5.6 Detail panel (`app/knowledge/node-panel.tsx`)

Top to bottom:

1. **Header.** Layer badge (Code/Rules/Learning, in the layer colour), kind label, title, and a
   copy-id action.
2. **Statement block.** For rules and incident learning. For a rule it also shows status and
   trust ("Approved by Param · 24 Oct").
3. **Attributes.** Path:line with a GitHub link via the shared URL builder, package, area,
   symbol_kind, language, category, confidence.
4. **Connections.** Grouped by edge kind ("Calls 12 · Called by 31 · Evidence 3 · Consulted by 4
   runs"). Each row is clickable and focuses that node. "Show all" opens the next 20.
5. **Vector.** Embedding model, dimensions, and the 5 nearest neighbours by meaning (clickable).
   If there is no embedding, it says why ("Not embedded: code symbols are matched by structure,
   not meaning").
6. **Learned → used.** For a learned item, a lineage line such as "Learned by Responder in run
   A (step investigate, 24 Oct 14:02) → used by Builder in run B, by Responder in run C". Each
   run is a link.
7. **Provenance.** Proposed or written by (agent avatar + role), run (link to the run in
   Workflows/Factory), approved by, when, edited from vN. The provenance timeline is shown when
   there is more than one event.
8. **Actions** (by permission and kind):
   - "Scan for rules here" for an area, package or class (starts the workflow and shows a cost
     preview first).
   - Approve / Edit / Reject for a proposed rule.
   - "Open in Factory run" for learning.

### 5.7 Rules review (`app/knowledge/rules-review.tsx`)

- **Queue.** Filters: status, scan, area. Each card shows title, statement, category,
  confidence, proposed-by and run, and evidence chips. A chip opens the code at those lines and
  focuses the symbol in the graph.
- **Actions.** Approve / Edit / Reject. Edit is an inline form for statement, title, category
  and scope. Reject asks for an optional note.
- **Bulk approve.** "Approve 5 selected" is shown with a count and a confirm step.
- **Gating.** The actions use `GatedButton` (`components/gated.tsx`). Without `knowledge:review`
  they are disabled, with a reason.
- **Facts sub-tab.** Holds today's fact promotion queue.

### 5.8 States (each one designed and tested; none unstyled)

| State | Shown |
|---|---|
| loading | skeleton tree rows + canvas shimmer (no spinner-only screen); > 1.5 s shows "Reading 51,319 nodes…" |
| empty: no repository | illustration-free card "Connect a repository to see its graph" → Repo and models |
| empty: indexing | progress from `/v1/knowledge/repositories` ("Indexing 4,380 files"), refreshes |
| empty layer | e.g. Rules chip with 0 rules: "No rules yet. Scan an area for business rules" + button |
| error | inline banner with the server's words + Retry; the other panels keep working |
| stale rollup | badge "Graph is from the last index (2 h ago)" + Rebuild |
| large neighbourhood | truncation note + Show more; never draws > 300 |
| locked (entitlement) | `LockedPanel` explaining the add-on |
| archived project | read-only banner; scan/approve disabled with reason |

### 5.9 Archive in the console

- The project menu (`app/project-menu.tsx`) gets **Archive project…**, with a confirm dialog
  that names what stops.
- The project strip gets a "Show archived" toggle. Archived projects show a badge and
  **Restore**.

### 5.10 Accessibility

- Every canvas has an `aria-label` and a visually hidden live region announcing the selection
  ("Selected class LoanWritePlatformServiceJpaRepositoryImpl, 180 connected classes").
- A "List view" toggle renders the current view as a table (nodes, edges, weights). It is
  available to screen-reader and keyboard users, and to tests.
- Colour is never the only signal: the kind name is in the tooltip, the legend and the detail
  panel.
- Contrast ≥ 4.5:1 for text on `--graph`, which the tester checks.
- Focus outlines use console tokens.

---

## 6. Build plan

**Rules for every stream.**
- Targeted tests only during development.
- Full suites once at the end, as the release gate.
- Each stream ends with an **independent tester agent** walking its screens in a real browser on
  a running stack. The stack is rebuilt (`boss-community-web-1`), because a console change is
  invisible until then.
- Findings are fixed, and the tester walks again.

**Shared files.** One owner per file. `db/schema.sql` blocks are append-only, one marked block
per stream, merged in the order P0 → S1 → S2 → S5.

### Phase 0 — contracts (one agent, serial, about half a day)

**Scope.** This phase lands first so the streams cannot collide:
- API response types: `packages/api-client` and `apps/web/lib/kg-view-types.ts`.
- Fixtures: `apps/web/tests/fixtures/kg-*.json`, taken from real Fineract rows.
- Area registration: `lib/area-ids.ts`, `ConsoleShell.tsx` AREAS, and a `page.tsx` mount of an
  empty styled `<GraphArea>`.
- Python signatures for `rules.propose_rule`, `learning_writeback.write_back` and
  `view_rollup.rebuild`, as stubs that raise `NotImplementedError`.

**Tests.** The area tests (`every-area-is-actually-visible`, `every-in-console-link-names-a-real-area`).

### Parallel streams (after P0)

| Stream | Owns (only these files) | Targeted tests | Tester walk |
|---|---|---|---|
| **S1 View read path** | `services/knowledge/view_rollup.py`, `services/knowledge/view.py`, `apps/api/app/routers/knowledge_view.py` (overview, neighbourhood, node, tree, search), schema block "KGV view rollups", one call in `repo_index.py _finish` + `ingest.reconcile_projections` | `engine/tests/test_knowledge_view_db.py` (area rule, tree, rollup equals live aggregation on a fixture, neighbourhood depth/limit/layers), `test_knowledge_view_perf_db.py` (opt-in, targets §3) | API-only (curl script in the walk notes) |
| **S2a Model + rules** | `store.py` (kinds, actor_ref), `scopes.py` (rules layer, product layer), `age.py` (label, property), schema block "KGR rules + usage" (generated column, kg_usage/kg_audit columns), `services/knowledge/rules.py`, `apps/api/app/routers/knowledge_rules.py` (review actions, a sibling router so S1 stays parallel), `tools/knowledge_tools.py` (kg_propose_rule, kg_rules), `packages/identity/catalogue.yaml` (+codegen) | `test_knowledge_rules_db.py` (agent cannot approve; evidence must resolve; edit supersedes; approve writes approved_by + audit actor_ref; corrects), `test_knowledge_stage_scopes_db` (updated) | — |
| **S2b Learning while executing** (the centre) | `services/knowledge/learning_writeback.py` (record, dedupe, confidence cap, event), `record_learning` tool registration (a new `tools/learning_tools.py`, not knowledge_tools.py, to avoid colliding with S2a), `service.pack` + `runtime.py` (Rules in scope, Learned before, usage kinds), `capture.py`/`ingest.py` (consulted/reused edges from usage), `typed_decisions.py` (cited_*_ids, `learned[]`), **one-line hook calls** in `governed_code_task_service.py` (stage boundary, repair checkpoint), `workflows.py complete_step`, `harness_failures.py` (turn pairs), `agent_service.py decide`; the primer text in `boss.fundamentals`; `apps/api/app/routers/knowledge_learning.py` (`/view/live`) | `test_learning_writeback_db.py` (each hook writes the right kind with provenance; dedupe increments; secret refused; < 0.4 not injected), `test_knowledge_consulted_db.py` (usage before the run node → edges at ingest; idempotent), `test_learning_reuse_proof_db.py` (§4.6 plumbing), battle-test scenarios "agent records what it ruled out", "agent consults a prior test_failure" | Run view strip and Graph update live during a scripted run |
| **S3 Scan workflow** | `workflow_capabilities.py` (scan_business_rules), `workflow_catalogue.py` (business_rules_scan), `decision_actions.py` (review_proposed_rules), scan executor module `services/knowledge/rule_scan.py` | `test_rule_scan.py` with the scripted conformance engine (budget reserve before call, stops at each limit, report shape; no paid model), catalogue validation test | Starts a scan from the console once S4 lands |
| **S4 Graph area UI** | `apps/web/app/knowledge/*` (`graph-area.tsx`, `graph-canvas.tsx`, `project-tree.tsx`, `node-panel.tsx`, `rules-review.tsx`, `kg-settings.tsx`, `learned-strip.tsx`, `kg.css`), a one-line mount of `learned-strip` in `app/factory/cockpit.tsx` and the workflow run view, `apps/web/lib/kg-layout.ts`, `apps/web/lib/kg-view.ts`; move of `knowledge-console.tsx` pieces; `page.tsx` only inside the P0 mount | `tests/kg-layout.test.ts` (deterministic, fit maths, arrow geometry, edge width clamp 1.5–3 px), `tests/kg-tree.test.tsx` (lazy load, search filter, ARIA), `tests/kg-node-panel.test.tsx` (every section, permission gating), `tests/kg-states.test.tsx` (every §5.8 state), parity test vs the old explorer | Full §5 walk on Fineract + boss-dogfood, both widths, keyboard-only pass |
| **S5 Archive project** | schema block "projects archive", `apps/api/app/routers/projects_archive.py`, `main.py` list filter (one WHERE clause), `services/projects_archive.py` guard + scheduler call sites, `app/project-menu.tsx`, `app/project-strip.tsx` | `test_projects_archive_db.py` (hidden from list, 409 on new work, every scheduler skips — enforcing grep test), `tests/project-archive.test.tsx` | Archive → gone from picker → restore |

Fixtures make S4 independent of S1/S2 until integration. S3 depends only on the P0 signature of
`propose_rule`. S2b depends on the S2a schema block (kinds, `kg_usage` columns), so S2a lands
its schema block first, on day one. S2b's hook calls are single lines into hot files, and each
is merged as its own small commit, so a conflict is trivial.

### Phase 3 — integration and the learning loop (one agent + tester)

**Wiring.** Wire S4 to the real routes and add `knowledge` to `tests/e2e/harness/console_contract.py`.

**Loop seed.** Seed the learning loop on a fresh project with the scripted conformance engine:
1. Scout scans `fineract-loan` and proposes 5 rules.
2. A person approves 4, edits 1 and rejects 0.
3. Builder runs a minor feature that consults 2 rules. A repair round records `test_failure` →
   `fix`.
4. A second build on the same symbols consults that `test_failure`.
5. Responder handles an incident. It records investigation (tried / ruled out / found) → root
   cause → fix → precedent → a rule correction.
6. A second incident reuses the precedent.

**Then for real.** Once the scripted loop passes, the case-study runs are created by talking to
BOSS's agents (§4.7), and the §4.6 pairs are measured.

**Check.** Assert every node and edge from §2.2 exists with provenance
(`engine/tests/test_knowledge_learning_loop_db.py`). Then one real agent run on one engine, then
the E2E gate (plumbing ≤ 5 min + one real build).

### 6.4 Demo readiness checklist

- [ ] Fineract project: overview < 300 ms and Focus < 200 ms, measured on the installed stack
      (not only in tests).
- [ ] U1–U9 walked by the independent tester on the release branch. Screenshots of every §5.8
      state.
- [ ] No unstyled screen. No literal colours outside `kg.css`. Text sizes per §5.3.
- [ ] The parity table for the moved knowledge screens is green.
- [ ] The learning loop is visible end to end: consulted, reused and approved_by edges are
      clickable, and provenance shows real agent, run and person.
- [ ] Learnings appear live: a run's "Learned so far" strip and the Graph show a new learning
      within 10 s of the agent recording it.
- [ ] The §4.6 proof is recorded with run ids, A vs A′, whichever way it came out.
- [ ] The case-study runs were created through BOSS's agents, and the gap tally is attached (§4.7).
- [ ] Archive/restore works, and an archived project's scheduler is silent for 15 min (log check).
- [ ] The scan respects its budget. The spend is shown in the run report and on the decision card.
- [ ] The fix is live in the running container on the demo host, not only on main.

### 6.5 Video plan

- **Console only.** Metatron appears once: asking "what rules govern loan disbursement?" and
  getting an answer with `[R:]` citations.
- **Story** (8–10 min):
  1. Problem: an agent breaks a rule nobody wrote down.
  2. Whole repo of Fineract.
  3. Search → Focus `disburseLoan`.
  4. Scout scan, with the budget visible.
  5. Rules review approve/edit.
  6. Builder run → its `consulted` edges.
  7. Incident → Responder RCA, with learnings appearing live as it rules things out → root cause,
     fix and precedent nodes.
  8. Second incident `reused` the precedent, with the step/token/time comparison on screen.
  9. Archive the old project.
- **Recording.** Record from a fresh run, after validating on an old one.
- **Thumbnail.** Designed, not a screen grab: the Focus view with the amber rule node and its
  arrows, and the title "Your code, its rules, what it learned".

---

## 7. Open questions (minimal)

1. **Packaging.** Is Rules + Learning + scan the paid add-on (`knowledge.rules`), with the Code
   view free? This spec assumes yes.
2. **Colours.** Confirm the proposed colours for the kinds not in the operator's list (§5.3).
3. **Domain reviewer.** Should rule approval also be offered to a non-admin "Domain reviewer"
   preset by default, or only to owners?
