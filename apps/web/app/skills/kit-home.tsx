"use client";

/**
 * Skills & MCP, what your crew can do (docs/design/SKILLS_AND_MCP_HOME.md).
 *
 * ONE HOME, TWO SECTIONS. MCP servers (each row: status, tools, who has it, the last error, Test,
 * Edit, Remove) and Skills (by outcome, the Kit's abilities with the Essentials strip; or All
 * skills). Add MCP server and Add skill are in the page head, on screen at every width. Who can,
 * Templates and Rules are small buttons at the right: reachable, out of the way. Old links
 * (`&tab=kit|library`, `&lib=mcp`) land on the new places (kit-url.ts).
 */
import { useGate, GatedButton } from "../../components/gated";
import { ReadFailure } from "../../components/locked-panel";
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowClockwise, ArrowLeft, DotsThree, Lightning, MagnifyingGlass, Plugs, ShieldCheck, Sparkle,
         SquaresFour, UsersThree } from "@phosphor-icons/react";

import type { Ability } from "../../lib/kit-types";
import { kitApi, type CrewMember, type KitWithCrew } from "../../lib/kit-api";
import { showToast } from "../client-forms";
import { useTips } from "../workflows/flow/tip-layer";
import { AbilityCard, type CardAction, type CardPermissions } from "./ability-card";

/** The card buttons that write: Test runs the ability (skill:invoke), Turn on enables it (skill:manage). */
const CARD_PERMISSIONS: CardPermissions = { test: "skill:invoke", turn_on: "skill:manage" };
import { AbilityDrawer, type DrawerFocus } from "./ability-drawer";
import { crewKey, dismissKey, EssentialsStrip, readFlag, writeFlag } from "./essentials-strip";
import { GROUP_LABEL, plural, shortError, TIP } from "./kit-copy";
import { KIT_ADD_EVENT, readKitParams, writeKitParams, type KitParams, type KitTab, type SkillsView } from "./kit-url";
import { askBossAboutSkills, Library, type LibraryRequest } from "./library";
import { AddMcpSheet } from "./add-mcp-sheet";
import { AddSkillSheet } from "./add-skill-sheet";
import { McpServersSection } from "./mcp-servers";
import { TestCrewModal } from "./test-crew-modal";
import { WhoCan } from "./who-can";
import { KitTemplates } from "../../components/v2/templates/kit-templates";
import { SkillAccessRules } from "./skill-access-rules";
import { ToolRules } from "./tool-rules";

export function KitHome({ projectId, actor }: { projectId: string; actor: string }) {
  useTips();
  // THE URL IS READ AFTER MOUNT, not during render: the server renders without it, and a first
  // client render that disagreed with the server's HTML threw the tree away (hydration).
  const [initial, setInitial] = useState<KitParams | null>(null);
  const [tab, setTab] = useState<KitTab>("home");
  const [view, setView] = useState<SkillsView>("outcome");
  const [adding, setAdding] = useState<"mcp" | "skill" | null>(null);
  const [openMcp, setOpenMcp] = useState<string | null>(null);
  const [mcpVersion, setMcpVersion] = useState(0);
  const [skillRequest, setSkillRequest] = useState<LibraryRequest | null>(null);
  const [kit, setKit] = useState<KitWithCrew | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errorStatus, setErrorStatus] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const [focus, setFocus] = useState<DrawerFocus>(null);
  const [testing, setTesting] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const [menu, setMenu] = useState(false);
  const [crewOpen, setCrewOpen] = useState(false);
  const [dismissed, setDismissed] = useState<boolean>(true);
  useEffect(() => {
    const params = readKitParams();
    setInitial(params);
    setTab(params.tab);
    setView(params.view);
    setOpen(params.ability);
    setOpenMcp(params.mcp);
    setAdding(params.add);
    // A link to the servers (`&lib=mcp`, `&section=mcp`) lands on them.
    if (params.section) {
      window.setTimeout(() => document.getElementById(`kit-${params.section}-section`)?.scrollIntoView?.({ block: "start" }), 50);
    }
    setDismissed(!params.setup && (readFlag(dismissKey(projectId)) || readFlag(crewKey(projectId))));
  }, [projectId]);

  // A LINK FOLLOWED INSIDE THE CONSOLE (Get started's "Connect your tools", the agent page's
  // "New skill") is a pushState + popstate on a page already mounted: honour what it asks.
  useEffect(() => {
    const on = () => {
      const here = new URLSearchParams(window.location.search);
      if (here.get("area") !== "skills") return;
      const params = readKitParams();
      if (params.add) { setTab("home"); setAdding(params.add); }
      if (params.mcp) { setTab("home"); setOpenMcp(params.mcp); }
      if (params.tab !== "home") setTab(params.tab);
    };
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);

  const load = useCallback(async (check: "stale" | "none" = "stale") => {
    const r = await kitApi.load(projectId, check);
    if (!r.ok) { setError(r.error); setErrorStatus(r.status ?? 0); return; }
    setError(null);
    setKit(r.data);
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);

  const replace = useCallback((a: Ability) => {
    setKit((k) => {
      if (!k) return k;
      const abilities = k.abilities.map((x) => (x.key === a.key ? a : x));
      return { ...k, abilities, ready: abilities.filter((x) => x.status === "ready").length };
    });
  }, []);

  const crew: CrewMember[] = kit?.crew ?? [];
  const opened = kit?.abilities.find((a) => a.key === open) ?? null;

  const openAbility = useCallback((key: string | null, why: DrawerFocus = null) => {
    setOpen(key); setFocus(why);
    writeKitParams({ ability: key, ...(key ? { tab: null, view: null } : {}) });
    if (key) { setTab("home"); setView("outcome"); }
  }, []);

  const showMcp = useCallback((id: string | null) => { setOpenMcp(id); writeKitParams({ mcp: id }); }, []);
  const openAdd = useCallback((kind: "mcp" | "skill") => { setAdding(kind); writeKitParams({ add: kind }); }, []);
  const closeAdd = useCallback(() => { setAdding(null); writeKitParams({ add: null, lib: null }); }, []);
  // Search (⌘K) and other screens open a sheet on this page while it is already mounted.
  useEffect(() => {
    const on = (e: Event) => {
      const kind = (e as CustomEvent).detail;
      if (kind === "mcp" || kind === "skill") { setTab("home"); setAdding(kind); }
    };
    window.addEventListener(KIT_ADD_EVENT, on);
    return () => window.removeEventListener(KIT_ADD_EVENT, on);
  }, []);

  const testGate = useGate("skill:invoke");
  const manageGate = useGate("skill:manage");
  const runTest = useCallback(async (key: string) => {
    // A card's Test / Turn on: refused up front, in words, for a viewer who cannot (the API decides).
    if (!testGate.allowed) { showToast("error", testGate.reason); return; }
    setTesting((t) => new Set(t).add(key));
    const r = await kitApi.test(projectId, actor, key);
    setTesting((t) => { const n = new Set(t); n.delete(key); return n; });
    if (!r.ok) { showToast("error", `Test failed, ${shortError(r.error)}`); return; }
    if (r.data.ok) showToast("success", `Test passed, ${r.data.fact ?? ""}`.trim());
    else showToast("error", `Test failed, ${shortError(r.data.error)}`);
    replace(r.data.ability);
  }, [projectId, actor, replace, testGate.allowed, testGate.reason]);

  const act = useCallback(async (key: string, action: CardAction) => {
    if (action === "test") return void runTest(key);
    if (action === "agents") return openAbility(key, "agents");
    if (action === "connect" || action === "fix") return openAbility(key, "connection");
    if (action === "review") return openAbility(key, "tools");
    if (action === "turn_on") {
      if (!manageGate.allowed) { showToast("error", manageGate.reason); return; }
      const r = await kitApi.enabled(projectId, actor, key, true);
      if (!r.ok) { showToast("error", shortError(r.error)); return; }
      showToast("success", `${r.data.label} on`);
      replace(r.data);
    }
  }, [runTest, openAbility, projectId, actor, replace, manageGate.allowed, manageGate.reason]);

  const testAll = async () => {
    const keys = (kit?.abilities ?? []).filter((a) => !["setup", "needs_repo", "off"].includes(a.status)).map((a) => a.key);
    setTesting(new Set(keys));
    const r = await kitApi.testAll(projectId, actor);
    setTesting(new Set());
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    const failed = Object.values(r.data.tests).filter((t) => !t.ok).length;
    showToast(failed ? "error" : "success", failed ? `Test failed, ${plural(failed, "ability", "abilities")}` : "Test passed, all");
    setKit(r.data.kit);
  };

  const applyRecommended = async () => {
    const r = await kitApi.recommended(projectId, actor);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", `Applied recommended, ${plural(r.data.granted.length, "grant")}`);
    void load("none");
  };

  const switchTab = (next: KitTab) => {
    setTab(next);
    writeKitParams({ tab: next === "home" ? null : next, lib: null, ability: next === "home" ? open : null });
  };
  const switchView = (next: SkillsView) => {
    setView(next);
    writeKitParams({ view: next === "all" ? "all" : null, lib: null, skill: null });
  };

  const essentials = useMemo(() => (kit ? kit.essentials.map((k) => kit.abilities.find((a) => a.key === k)).filter(Boolean) as Ability[] : []),
                              [kit]);
  const showStrip = !dismissed && essentials.length > 0;
  const shown = (kit?.abilities ?? []).filter((a) => !query.trim()
    || `${a.label} ${a.tip}`.toLowerCase().includes(query.trim().toLowerCase()));

  const backHome = () => switchTab("home");
  const Nav = ({ id, label, tip, icon }: { id: KitTab; label: string; tip: string; icon: React.ReactNode }) => (
    <button type="button" className="kit-btn kit-btn-sm kit-btn-ghost" aria-pressed={tab === id} data-tip={tip}
            aria-label={label} data-testid={`kit-tab-${id}`} onClick={() => switchTab(tab === id ? "home" : id)}>
      {icon}<span className="kit-btn-word">{label}</span>
    </button>
  );

  return (
    <div className="kit-home" data-testid="kit-home" data-drawer={opened && tab === "home" && view === "outcome" ? "open" : undefined}>
      <div className="kit-head kit-page-head">
        {tab !== "home" ? (
          <button type="button" className="kit-btn kit-btn-sm kit-btn-ghost" data-testid="kit-back" onClick={backHome}>
            <ArrowLeft size={14} aria-hidden="true" /> Skills & MCP
          </button>
        ) : null}
        <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-primary" data-testid="kit-add-mcp-btn"
                     data-tip="Connect a tool server your agents can call" onClick={() => openAdd("mcp")}>
          <Plugs size={14} weight="bold" aria-hidden="true" /> Add MCP server
        </GatedButton>
        <button type="button" className="kit-btn" data-testid="kit-add-skill-btn" data-tip="Write, import or turn on a skill"
                onClick={() => openAdd("skill")}>
          <Sparkle size={14} weight="fill" aria-hidden="true" /> Add skill
        </button>
        <span className="kit-spacer" />
        {tab === "home" ? (
          <label className="kit-search">
            <MagnifyingGlass size={14} aria-hidden="true" />
            <input type="search" value={query} placeholder="Find" aria-label="Find a server or skill"
                   data-testid="kit-search" onChange={(e) => setQuery(e.target.value)} />
          </label>
        ) : null}
        <nav className="kit-nav" aria-label="More views">
          <Nav id="who" label="Who can" tip="Every agent and what it may use" icon={<UsersThree size={14} aria-hidden="true" />} />
          <Nav id="templates" label="Templates" tip="App templates: import, review, approve, trial, hide" icon={<SquaresFour size={14} aria-hidden="true" />} />
          <Nav id="rules" label="Rules" tip="Tool rules and skill access: deny, ask, allow, rate-limit" icon={<ShieldCheck size={14} aria-hidden="true" />} />
        </nav>
      </div>

      {tab === "home" ? (
        <>
          <McpServersSection projectId={projectId} actor={actor} crew={crew} abilities={kit?.abilities ?? []} query={query}
                             openId={openMcp} onOpen={showMcp} onAdd={() => openAdd("mcp")} version={mcpVersion}
                             onKitChanged={() => void load("none")} />

          <section className="kit-sec" aria-label="Skills" data-testid="kit-skills-section" id="kit-skills-section">
            <header className="kit-sec-head">
              <h2 className="kit-sec-title" data-tip="Reusable things agents and workflow steps can do">
                <Lightning size={16} weight="duotone" aria-hidden="true" className="kit-icon" /> Skills
              </h2>
              <span className="kit-seg kit-seg-sm" role="radiogroup" aria-label="Show skills">
                <button type="button" role="radio" aria-checked={view === "outcome"} data-testid="kit-skills-outcome"
                        data-tip="Grouped by what they get done" onClick={() => switchView("outcome")}>By outcome</button>
                <button type="button" role="radio" aria-checked={view === "all"} data-testid="kit-skills-all"
                        data-tip="Every skill, yours and built in" onClick={() => switchView("all")}>All skills</button>
              </span>
              <span className="kit-spacer" />
              {view === "outcome" && kit ? (
                <>
                  <span className="kit-progress" data-tip={TIP.progress} data-testid="kit-progress">{kit.ready} of {kit.total} ready</span>
                  <GatedButton permission="skill:invoke" type="button" className="kit-btn kit-btn-sm" data-tip={TIP.testAll} data-testid="kit-test-all"
                               disabled={testing.size > 0} onClick={() => void testAll()}>
                    {testing.size > 0 ? <span className="kit-spin" aria-hidden="true" /> : <ArrowClockwise size={14} aria-hidden="true" />} Test all
                  </GatedButton>
                  <span className="kit-confirm-wrap">
                    <button type="button" className="kit-icon-btn" aria-label="More for skills" data-tip="More" data-testid="kit-head-more"
                            aria-expanded={menu} onClick={() => setMenu((m) => !m)}><DotsThree size={18} /></button>
                    {menu ? (
                      <span className="kit-pop kit-menu" role="menu">
                        <GatedButton permission="agent_ability:assign" type="button" role="menuitem" data-tip={TIP.applyRecommended}
                                     data-testid="kit-apply-recommended" onClick={() => { setMenu(false); void applyRecommended(); }}>Apply recommended</GatedButton>
                        <GatedButton permission="skill:invoke" type="button" role="menuitem" data-testid="kit-menu-test-crew"
                                     onClick={() => { setMenu(false); setCrewOpen(true); }}>Test crew</GatedButton>
                      </span>
                    ) : null}
                  </span>
                </>
              ) : null}
            </header>

            {view === "outcome" ? (
              <>
                {error && !kit ? (
                  <ReadFailure problem={{ text: error, status: errorStatus }} onRetry={() => void load()} testId="kit-api-error" />
                ) : null}
                {!kit ? (
                  error ? null : (
                    <div className="kit-grid" data-testid="kit-loading" aria-busy="true">
                      {Array.from({ length: 8 }).map((_, i) => <div key={i} className="kit-card kit-skeleton" />)}
                    </div>
                  )
                ) : (
                  <div className="kit-body">
                    <div className="kit-main">
                      {showStrip ? (
                        <EssentialsStrip essentials={essentials} testing={testing}
                                         onOpen={(k) => openAbility(k)}
                                         onAction={(k, a) => void act(k, a)}
                                         onTestCrew={() => setCrewOpen(true)}
                                         onDismiss={() => { writeFlag(dismissKey(projectId), true); setDismissed(true); writeKitParams({ setup: null }); }} />
                      ) : null}
                      {query.trim() && shown.length === 0 ? (
                        <div className="kit-list-empty" data-testid="kit-empty">
                          <span>No matches</span>
                          <button type="button" className="kit-link" onClick={() => setQuery("")}>Clear</button>
                        </div>
                      ) : null}
                      {kit.groups.map((g) => {
                        const cards = shown.filter((a) => a.group === g);
                        if (cards.length === 0) return null;
                        return (
                          <section key={g} className="kit-group" aria-label={GROUP_LABEL[g] ?? g} data-testid={`kit-group-${g}`}>
                            <h3 className="kit-group-label">{GROUP_LABEL[g] ?? g}</h3>
                            <div className="kit-grid">
                              {cards.map((a) => (
                                <AbilityCard key={a.key} ability={a} testing={testing.has(a.key)}
                                             onOpen={() => openAbility(a.key)} onAction={(x) => void act(a.key, x)}
                                             permissions={CARD_PERMISSIONS} />
                              ))}
                            </div>
                          </section>
                        );
                      })}
                    </div>
                    {opened ? (
                      <AbilityDrawer key={opened.key} ability={opened} crew={crew} projectId={projectId} actor={actor}
                                     focus={focus} testing={testing.has(opened.key)}
                                     onClose={() => openAbility(null)} onChanged={replace} onReload={() => void load("none")} />
                    ) : null}
                  </div>
                )}
              </>
            ) : initial ? (
              <Library projectId={projectId} actor={actor} kit={kit} crew={crew} initial={initial} query={query}
                       onClearQuery={() => setQuery("")}
                       request={skillRequest} onRequestDone={() => setSkillRequest(null)}
                       onKitChanged={() => void load("none")} />
            ) : null}
          </section>
        </>
      ) : null}

      {tab === "who" ? (
        kit ? <WhoCan kit={kit} crew={crew} projectId={projectId} actor={actor} focusRole={initial?.role ?? null}
                      onChanged={replace} onReload={() => void load("none")}
                      onOpenAbility={(k) => { switchTab("home"); openAbility(k); }} />
            : error ? <ReadFailure problem={{ text: error, status: errorStatus }} onRetry={() => void load()} testId="kit-api-error" />
            : <div className="kit-grid" aria-busy="true"><div className="kit-card kit-skeleton" /></div>
      ) : null}

      {tab === "templates" ? <KitTemplates projectId={projectId} /> : null}

      {/* BOSSAPP-815/817 and 890: the ordered tool rules with Always allow, then skill access (its own heading). */}
      {tab === "rules" ? (
        <div className="kit-rules" data-testid="kit-rules">
          <h2 className="kit-sec-title" data-tip="Deny, ask, allow or rate-limit tool calls, in order; Always allow">
            <ShieldCheck size={16} weight="duotone" aria-hidden="true" className="kit-icon" /> Tool rules
          </h2>
          <ToolRules projectId={projectId} actor={actor} />
          <SkillAccessRules projectId={projectId} />
        </div>
      ) : null}

      {adding === "mcp" ? (
        <AddMcpSheet projectId={projectId} actor={actor} crew={kit?.crew} onClose={closeAdd}
                     onAdded={(id) => { closeAdd(); setMcpVersion((v) => v + 1); showMcp(id); void load("none"); }} />
      ) : null}
      {adding === "skill" ? (
        <AddSkillSheet onClose={closeAdd} onPick={(way) => {
          closeAdd();
          if (way === "describe") { askBossAboutSkills(projectId); return; }
          switchTab("home"); switchView("all"); setSkillRequest(way);
        }} />
      ) : null}

      {crewOpen && kit ? (
        <TestCrewModal projectId={projectId} actor={actor} abilities={kit.abilities}
                       onClose={() => setCrewOpen(false)}
                       onDone={(passed) => {
                         if (passed) { writeFlag(crewKey(projectId), true); setDismissed(true); }
                         void load("none");
                       }}
                       onOpenAbility={(k) => { setCrewOpen(false); openAbility(k); }} />
      ) : null}
    </div>
  );
}
