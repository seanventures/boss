# Skills & MCP: one simple home

Tickets: BOSSAPP-1477 (one home, Add MCP server from the first screen) and BOSSAPP-1478
(the same add flow from the agent page, the create-agent wizard, the workflow step editor,
search and Get started).

## The problem

Adding an MCP server took four clicks: **Skills & MCP › Library › MCP › Add MCP**. The page
opened on the Kit (abilities by outcome). MCP servers were the second option of a switch inside
the third tab. Skills had the same problem: **New skill** was also inside Library.
The add sheet had three tabs (Catalogue, Connectors, Custom), so there were three forms.

## The new design, in one picture

The page opens on two sections, **MCP servers** and **Skills**. Both add buttons are in the page
head, so they are on screen at every width.

```
 Skills & MCP
 ┌─────────────────────────────────────────────────────────────────────────────────────────┐
 │ [⊕ Add MCP server]  [✦ Add skill]           [Find…        ]   [👥 Who can] [▦ Templates] [🛡 Rules] │
 └─────────────────────────────────────────────────────────────────────────────────────────┘
 ┌ 🔌 MCP servers · 3 ────────────────────────────────────────────────────── [⋯] ┐
 │ ● web-intel   Built in   4 tools  (J)(A)                    [↻ Test] [✎ Edit] [🗑] │
 │ ● context7    Official   2 tools  —        Review                [↻ Test] [✎ Edit] [🗑] │
 │ ● tracker     Custom     0 tools  (J)      ⚠ Token rejected       [↻ Test] [✎ Edit] [🗑] │
 │                                                            Show all 9 ▾            │
 └──────────────────────────────────────────────────────────────────────────────────┘
 ┌ ⚡ Skills ─────────────── [By outcome | All skills]  8 of 12 ready  [↻ Test all] [⋯] ┐
 │ Essentials  ① Open PRs ● ② Research ● ③ Tickets ○ ④ Email ○                         │
 │ Code       [card] [card] [card]                                                     │
 │ Research   [card] [card]                                                            │
 └──────────────────────────────────────────────────────────────────────────────────┘
```

* `⋯` on **MCP servers**: Test all servers, Import, Export, Gateways.
* `⋯` on **Skills**: Apply recommended, Test crew, Import, Export.
* **All skills** is the old Library skills list: filters (Yours, Built in, Show internal),
  rows with status, holders, ability, risk and "Used in", and the skill drawer.
* **Who can**, **Templates** and **Rules** are small ghost buttons at the right of the head.
  They are reachable but out of the way. Each opens its own view with a **Back** link.

Icons (`@phosphor-icons/react`, 14px in buttons, 16px in section heads):

| Place | Icon |
|---|---|
| Add MCP server (primary) | `Plugs` |
| Add skill | `Sparkle` |
| Who can | `UsersThree` |
| Templates | `SquaresFour` |
| Rules | `ShieldCheck` |
| MCP servers section head | `Plugs` |
| Skills section head | `Lightning` |
| Row: Test | `ArrowClockwise` |
| Row: Edit | `PencilSimple` |
| Row: Remove | `Trash` |
| Section menu | `DotsThree` |
| Test connection | `PlugsConnected` |
| Add server / Done | `Check` |
| Browse all connectors | `MagnifyingGlass` |
| Add skill options | `PencilSimple` Write · `DownloadSimple` Import · `Sparkle` Describe · `Package` Built in |
| Back from a sub-view | `ArrowLeft` |

At 768px and below, row actions show only their icon (with `aria-label` and a tooltip). At 400px
a row wraps to two lines: name and status, then tools, agents and actions.
Nothing scrolls sideways.

## Add MCP server: one screen

```
 ┌ Add MCP server ───────────────────────────────────────────────────── × ┐
 │ Popular   [Context7] [Linear] [GitHub] [Sentry] …   [🔍 Browse all]     │
 │                                                                          │
 │ URL or command  [ https://mcp.example.com/mcp  |  npx -y @acme/mcp   ]  │
 │ Name            [ example                                            ]  │
 │ Sign-in         ( None | Token | Header | OAuth )                        │
 │ Token           [ ••••••••••• ]  🔒 Saved to the vault                   │
 │ ▸ Advanced  (header name, risk, your own OAuth app)                      │
 │                                                                          │
 │ [🔌 Test connection]                                                     │
 │  ✓ Connected · 6 tools  [search] [fetch] [create_issue] …                │
 │  ✗ Token rejected  (full message in the tooltip)                         │
 │                                                                          │
 │ Give to agents  (J Juno) (A Atlas) (+ …)          ← chips, toggle        │
 │                                         [Cancel]  [✓ Add server]         │
 └──────────────────────────────────────────────────────────────────────────┘
```

* **One component** (`AddMcpSheet`), used by Skills & MCP, the agent page, the create-agent
  wizard and the workflow step editor. There is no second form.
* Pick a catalogue chip: it fills URL, name and sign-in. A token entry shows the Token field.
  **Browse all** opens the full connector list (the old Connectors tab) in the same sheet. Picking
  one fills the form. A local (stdio) connector still shows **Create template** first (Owner/Admin).
* **URL or command**: an `http(s)://` URL is a remote server. Anything else is read as a launch
  command, like `npx -y @scope/server` or `uvx pkg` or `docker run image`. The sheet then says
  "Commands run from a template" and links to **Browse all**, where the template is made.
  BOSS runs a command only from a reviewed template.
* **Secrets are vault references.** A typed token goes to the API once. The API writes it to
  the secret store and keeps only `secret_ref_id` and the last four characters. The sheet says
  "Saved to the vault" next to the field.
* **Test connection** runs `POST /v1/mcp/servers/test`. That call tests what was typed and stores
  nothing. The sheet lists the tools it found as chips, or one plain-words error with the full
  message in the tooltip.
* **Give to agents**: chips for the crew. Chips passed in (`defaultAgents`) start on. The wizard
  hides this row, because its agent does not exist yet; it ticks the new server on its own
  MCP list instead.
* **Add server** registers the server, then grants it to every chip that is on. A catalogue entry
  is added by its own call (`catalogue_add`), which tests and stores together. Its grants follow.
* OAuth: **Add & sign in** registers the server and opens the provider's sign-in in a new tab.

## Add skill: one screen

```
 ┌ Add skill ──────────────────────────────── × ┐
 │ [✎ Write one]      Name it, say how it runs   │
 │ [⇩ Import]         A file, YAML or a repo     │
 │ [✦ Describe]       BOSS drafts it with you    │
 │ [📦 Built in]      Turn on one BOSS ships     │
 └───────────────────────────────────────────────┘
```

**Write one** opens the skill drawer in create mode. After **Save**, the same drawer shows
**Agents** (give to agents), **Tests** (saved cases, run and graded) and **History**.

## Server and skill rows

| Field | MCP server row | Skill row (All skills) |
|---|---|---|
| Status pill | Ready / Failing / Review / Set up / Off / No one holds | same set |
| Count | N tools | Used in N |
| Who has it | avatar stack (agents granted) | avatar stack (holders) |
| Last error | plain words (`shortError`), full text in the tooltip | catalogue reason, in the tooltip of the status |
| Actions | Test · Edit · Remove | click opens the drawer (edit, test, agents, delete) |

## Places that open the same add flow (BOSSAPP-1478)

| From | Click 1 | Click 2 |
|---|---|---|
| Skills & MCP | **Add MCP server** (page head) | — |
| Agent page, Loadout › MCP | **Add ▾** | **Add MCP server** (opens the sheet, this agent pre-chosen) |
| Create-agent wizard › Skills and MCP | **Add MCP server** (under the MCP list) | — |
| Workflow step editor › Skills › MCP | **+ MCP** | **Add MCP server** (step's agent pre-chosen) |
| Search (⌘K) | type "mcp" | **Add MCP server** |
| Get started (welcome) | **Connect your tools (MCP)** → **Add MCP server** | — |

Search also offers **Add skill**.

## Old links

| Old link | Lands on |
|---|---|
| `?area=skills` | home (both sections) |
| `&tab=kit` | home |
| `&tab=library` / `&lib=skills` | home, Skills › All skills |
| `&tab=library&lib=mcp` | home, scrolled to MCP servers |
| `&skill=<key>` | home, All skills, that skill's drawer open |
| `&mcp=<id>` | home, that server's drawer open |
| `&lib=mcp&add=1` (old agent page link) | home with Add MCP server open |
| `&ability=<key>` | home, By outcome, that ability's drawer |
| `&tab=who&role=…`, `&tab=templates`, `&tab=rules` | those views, unchanged |
| `?area=capabilities` (retired) | home, MCP servers |
| new: `&add=mcp`, `&add=skill` | home with that sheet open |

## Parity table: every function and where it lives now

Nothing is removed. Options people rarely use are folded under `⋯` or **Advanced**.

| # | Function (before) | Where it was | Where it is now |
|---|---|---|---|
| 1 | Abilities by outcome (cards, status, one button each) | Kit tab | Skills › By outcome (default) |
| 2 | Essentials strip + dismiss + `&setup=1` | Kit tab | Skills › By outcome, top |
| 3 | "N of M ready" progress | Kit head | Skills section head |
| 4 | Test all abilities | Kit head | Skills section head (`ArrowClockwise`) |
| 5 | Apply recommended | Kit head + Who can | Skills `⋯` + Who can |
| 6 | Test crew | Kit `⋯` | Skills `⋯` |
| 7 | Find an ability | Kit head | page **Find** (filters both sections) |
| 8 | Ability drawer (connection, tools, agents, used in, try it) | Kit | unchanged, opens from a card |
| 9 | Who can matrix, Gaps, Undo, `&role=` focus | Who can tab | **Who can** button → view |
| 10 | App templates | Templates tab | **Templates** button → view |
| 11 | Tool rules (deny/ask/allow/rate, Always allow, suggested, simulate) | Rules tab | **Rules** button → view, "Tool rules" heading |
| 12 | Skill access rules + simulate | Rules tab | **Rules** view, "Skill access" heading |
| 13 | Skill list + filters Yours / Built in / Show internal | Library › Skills | Skills › All skills |
| 14 | New skill (create drawer) | Library › Skills | **Add skill › Write one** |
| 15 | Describe a skill (dock to PM) | Library head | **Add skill › Describe** |
| 16 | Import skill (YAML / source repo) | Library `⋯` | **Add skill › Import**, and Skills `⋯` |
| 17 | Export all skills | Library `⋯` | Skills `⋯` |
| 18 | Skill drawer: edit, history, tests, agents, used in, on/off, delete | Library | unchanged, from a skill row |
| 19 | MCP list (status, provenance, tools, risk, holders) | Library › MCP | **MCP servers** section (risk shown in the tooltip on the name) |
| 20 | Add from curated catalogue (token when needed) | Add MCP › Catalogue | Add MCP server › Popular chips |
| 21 | Connector catalog search + categories | Add MCP › Connectors | Add MCP server › **Browse all** |
| 22 | Create stdio template (host:exec) | Add MCP › Connectors | Add MCP server › Browse all (unchanged card button) |
| 23 | Custom server: name, URL, auth none/bearer/header/OAuth, header name, OAuth client, risk | Add MCP › Custom | Add MCP server (header name, risk, OAuth client under **Advanced**) |
| 24 | Test before store | Save & test | **Test connection** (lists tools), then **Add server** |
| 25 | Connector tool tiers/policies applied on add | Custom after pick | unchanged, same code path |
| 26 | Grant to agents | MCP drawer › Agents only | **Give to agents** chips in the sheet, and the drawer |
| 27 | MCP drawer: URL, auth, token replace, sign-in, headers, tools policy, review, agents, used in, try it, data boundary, on/off, remove | Library › MCP › row | row **Edit** (same drawer) |
| 28 | Remove server (confirm, releases grants) | drawer `⋯` | row **Remove** (confirm) + drawer `⋯` |
| 29 | Re-test a server | drawer Save & test | row **Test** (probes now, shows result) + drawer |
| 30 | Import MCP JSON | Library `⋯` (MCP) | MCP servers `⋯` › Import |
| 31 | Export MCP JSON (no secrets) | Library `⋯` (MCP) | MCP servers `⋯` › Export |
| 32 | Gateways (serve servers to Claude Code, Cursor) | Library `⋯` (MCP) | MCP servers `⋯` › Gateways |
| 33 | `&mcp=`, `&skill=`, `&ability=`, `&tab=` deep links | URL | all honoured (table above) |
| 34 | Agent page Loadout › MCP › Add ▾ › From Library | agent page | unchanged, plus **Add MCP server** (inline sheet) |
| 35 | Create-agent wizard MCP list + "Only these tools" | wizard | unchanged, plus **Add MCP server** |
| 36 | Step editor scope pickers | step editor | unchanged, plus **Add MCP server** in the MCP picker |
| 37 | Capabilities screen link | capabilities | link now reads "MCP servers, N ready" and opens the MCP section |

## Content review: every block of text

| Item | Where now | Decision | Why |
|---|---|---|---|
| Five tabs Kit / Who can / Library / Templates / Rules | page head | **Replace** with two sections + three small buttons | Kit and Library both listed skills. MCP was hidden inside Library. |
| Library segment Skills / MCP | Library head | **Remove (duplicate)** | Both are now sections on the home |
| "Add MCP" label | Library head | **Rename** "Add MCP server", primary, page head | It was hard to find and the label was vague |
| Add MCP tabs Catalogue / Connectors / Custom | add sheet | **Merge** into one screen: Popular chips, Browse all, URL field | There were three forms for one job |
| Catalogue card chips "Remote", "No key/Token", "Official" | add sheet | **Shorten**: the chip shows label + key icon; Official in the tooltip | Three chips per card was noise |
| Connector note "Needs OAuth sign-in, which is not available here yet." | add sheet | **Remove (wrong)**: OAuth is supported; the connector now preselects OAuth | Stale text |
| Risk (Reads / Writes / Public) on add | add sheet | **Move under Advanced** | Defaults come from the catalogue; rarely changed |
| Header name, OAuth client id/secret | add sheet | **Advanced** (header name shows when Header is picked) | Rare |
| "Save & test" | add sheet | **Split** into **Test connection** + **Add server** | People need to see the tools before saving |
| Token field | add sheet | **Keep**, add "Saved to the vault" with a lock icon | Says where the secret goes |
| "Describe" button | Library head | **Move** to Add skill | Duplicate entry point |
| "New skill" button | Library head | **Move** to Add skill › Write one | One add entry for skills |
| "Apply recommended" | Kit head and Who can head | **Fold** the Kit copy under Skills `⋯`; keep it in Who can | Duplicate |
| "Test crew" | Kit `⋯` | **Keep** in Skills `⋯` | Rare |
| Risk chip on each MCP row | Library MCP row | **Move** to the name's tooltip | It repeated the drawer; rows now carry Test/Edit/Remove |
| Provenance chip (Built in / Official / Custom) | rows | **Keep** | Answers "where did this come from" in one word |
| Empty-row text "No matches" | lists | **Keep** + **Clear** link | Short |
| MCP empty state | — | **Add**: icon + "No MCP servers yet" + Add MCP server | A fresh install showed an empty list with no next step |
| Rules tab (two stacked panels without headings) | Rules | **Keep** both; **add** headings "Tool rules" (`ShieldCheck`) and "Skill access" (`Key`) | You could not tell where one panel ended |
| Capabilities "Pluggable tool backends, JSON-RPC over HTTP, plug in external & org-specific servers" | capabilities | **Shorten** to "Tools agents call" | Jargon |
| Essentials strip and ability cards | Kit | **Keep** as is | Already label + tooltip |
| Drawer sections (Connection, Tools, Agents, Used in, Try it, More) | drawers | **Keep** | Already one line each, details in tooltips |
| Who can matrix | Who can | **Keep** | Bulk grants live here |

## How it is tested

* `tests/skills-and-mcp-home.test.tsx`: the home opens on both sections with **Add MCP server**
  visible. Legacy links redirect. Row Test, Edit and Remove work. Add skill offers four ways.
* `tests/add-mcp.test.tsx`: Catalogue, custom, test, grant and OAuth, with click-through
  add → test → grant.
* `tests/mcp-findable-everywhere.test.tsx`: from the agent page, the wizard, the step editor,
  search and Get started, **Add MCP server** is reached in at most two clicks, and it is the same
  component.
* The copy budget test covers the new sections and the sheet.
