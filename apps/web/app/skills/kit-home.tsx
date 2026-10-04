"use client";

/**
 * Skills & MCP — what your crew can do (skills-mcp spec §5).
 *
 * THREE TABS, ONE HOME PER JOB. `Kit` (default) is the abilities, grouped by outcome, each with
 * one status and one button, and on a fresh install the Essentials strip that takes it to "my
 * crew can open PRs, research the web, file tickets and send email". `Who can` is the crew ×
 * abilities matrix for grants in bulk. `Library` is every skill and MCP server, with authoring
 * and any custom server. Everything is driven by GET /v1/kit, whose statuses the API computes.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { DotsThree, MagnifyingGlass, Sparkle } from "@phosphor-icons/react";

import type { Ability } from "../../lib/kit-types";
import { kitApi, type CrewMember, type KitWithCrew } from "../../lib/kit-api";
import { showToast } from "../client-forms";
import { useTips } from "../workflows/flow/tip-layer";
import { AbilityCard, type CardAction } from "./ability-card";
import { AbilityDrawer, type DrawerFocus } from "./ability-drawer";
import { crewKey, dismissKey, EssentialsStrip, readFlag, writeFlag } from "./essentials-strip";
import { GROUP_LABEL, shortError, TIP } from "./kit-copy";
import { readKitParams, writeKitParams, type KitParams, type KitTab } from "./kit-url";
import { Library } from "./library";
import { TestCrewModal } from "./test-crew-modal";
import { WhoCan } from "./who-can";

export function KitHome({ projectId, actor }: { projectId: string; actor: string }) {
  useTips();
  // THE URL IS READ AFTER MOUNT, not during render: the server renders without it, and a first
  // client render that disagreed with the server's HTML threw the tree away (hydration).
  const [initial, setInitial] = useState<KitParams | null>(null);
  const [tab, setTab] = useState<KitTab>("kit");
  const [kit, setKit] = useState<KitWithCrew | null>(null);
  const [error, setError] = useState<string | null>(null);
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
    setOpen(params.ability);
    setDismissed(!params.setup && (readFlag(dismissKey(projectId)) || readFlag(crewKey(projectId))));
  }, [projectId]);

  const load = useCallback(async (check: "stale" | "none" = "stale") => {
    const r = await kitApi.load(projectId, check);
    if (!r.ok) { setError(r.error); return; }
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
    writeKitParams({ ability: key, tab: key ? "kit" : null });
    if (key) setTab("kit");
  }, []);

  const runTest = useCallback(async (key: string) => {
    setTesting((t) => new Set(t).add(key));
    const r = await kitApi.test(projectId, actor, key);
    setTesting((t) => { const n = new Set(t); n.delete(key); return n; });
    if (!r.ok) { showToast("error", `Test failed · ${shortError(r.error)}`); return; }
    if (r.data.ok) showToast("success", `Test passed · ${r.data.fact ?? ""}`.trim());
    else showToast("error", `Test failed · ${shortError(r.data.error)}`);
    replace(r.data.ability);
  }, [projectId, actor, replace]);

  const act = useCallback(async (key: string, action: CardAction) => {
    if (action === "test") return void runTest(key);
    if (action === "agents") return openAbility(key, "agents");
    if (action === "connect" || action === "fix") return openAbility(key, "connection");
    if (action === "review") return openAbility(key, "tools");
    if (action === "turn_on") {
      const r = await kitApi.enabled(projectId, actor, key, true);
      if (!r.ok) { showToast("error", shortError(r.error)); return; }
      showToast("success", "Saved");
      replace(r.data);
    }
  }, [runTest, openAbility, projectId, actor, replace]);

  const testAll = async () => {
    const keys = (kit?.abilities ?? []).filter((a) => !["setup", "needs_repo", "off"].includes(a.status)).map((a) => a.key);
    setTesting(new Set(keys));
    const r = await kitApi.testAll(projectId, actor);
    setTesting(new Set());
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    const failed = Object.values(r.data.tests).filter((t) => !t.ok).length;
    showToast(failed ? "error" : "success", failed ? `Test failed · ${failed} abilities` : "Test passed · all");
    setKit(r.data.kit);
  };

  const applyRecommended = async () => {
    const r = await kitApi.recommended(projectId, actor);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", `Applied recommended · ${r.data.granted.length} grants`);
    void load("none");
  };

  const switchTab = (next: KitTab) => {
    setTab(next);
    writeKitParams({ tab: next === "kit" ? null : next, ability: next === "kit" ? open : null });
  };

  const essentials = useMemo(() => (kit ? kit.essentials.map((k) => kit.abilities.find((a) => a.key === k)).filter(Boolean) as Ability[] : []),
                              [kit]);
  const showStrip = !dismissed && essentials.length > 0;
  const shown = (kit?.abilities ?? []).filter((a) => !query.trim()
    || `${a.label} ${a.tip}`.toLowerCase().includes(query.trim().toLowerCase()));

  return (
    <div className="kit-home" data-testid="kit-home" data-drawer={opened && tab === "kit" ? "open" : undefined}>
      <div className="kit-head">
        <div className="kit-tabs" role="tablist" aria-label="Skills & MCP">
          {(["kit", "who", "library"] as const).map((t) => (
            <button key={t} type="button" role="tab" aria-selected={tab === t} className="kit-tab"
                    data-testid={`kit-tab-${t}`} onClick={() => switchTab(t)}>
              {t === "kit" ? "Kit" : t === "who" ? "Who can" : "Library"}
            </button>
          ))}
        </div>
        {tab === "kit" && kit ? (
          <>
            <span className="kit-progress" data-tip={TIP.progress} data-testid="kit-progress">{kit.ready} of {kit.total} ready</span>
            <button type="button" className="kit-btn" data-tip={TIP.testAll} data-testid="kit-test-all"
                    disabled={testing.size > 0} onClick={() => void testAll()}>Test all</button>
            <button type="button" className="kit-btn" data-tip={TIP.applyRecommended} data-testid="kit-apply-recommended"
                    onClick={() => void applyRecommended()}><Sparkle size={14} weight="fill" /> Apply recommended</button>
            <span className="kit-confirm-wrap">
              <button type="button" className="kit-icon-btn" aria-label="More" data-testid="kit-head-more"
                      onClick={() => setMenu((m) => !m)}><DotsThree size={18} /></button>
              {menu ? (
                <span className="kit-pop kit-menu" role="menu">
                  <button type="button" role="menuitem" data-testid="kit-menu-test-crew"
                          onClick={() => { setMenu(false); setCrewOpen(true); }}>Test crew</button>
                </span>
              ) : null}
            </span>
            <span className="kit-spacer" />
            <label className="kit-search">
              <MagnifyingGlass size={14} aria-hidden="true" />
              <input type="search" value={query} placeholder="Find an ability" aria-label="Find an ability"
                     data-testid="kit-search" onChange={(e) => setQuery(e.target.value)} />
            </label>
          </>
        ) : null}
      </div>

      {error && !kit ? (
        <div className="kit-bar-err" role="alert" data-testid="kit-api-error">
          <span data-tip={error}>Can&apos;t reach BOSS API</span>
          <button type="button" className="kit-link" onClick={() => void load()}>Retry</button>
        </div>
      ) : null}

      {tab === "kit" ? (
        !kit ? (
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
              {kit.groups.map((g) => {
                const cards = shown.filter((a) => a.group === g);
                if (cards.length === 0) return null;
                return (
                  <section key={g} className="kit-group" aria-label={GROUP_LABEL[g] ?? g} data-testid={`kit-group-${g}`}>
                    <h3 className="kit-group-label">{GROUP_LABEL[g] ?? g}</h3>
                    <div className="kit-grid">
                      {cards.map((a) => (
                        <AbilityCard key={a.key} ability={a} testing={testing.has(a.key)}
                                     onOpen={() => openAbility(a.key)} onAction={(x) => void act(a.key, x)} />
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
        )
      ) : null}

      {tab === "who" ? (
        kit ? <WhoCan kit={kit} crew={crew} projectId={projectId} actor={actor} focusRole={initial?.role ?? null}
                      onChanged={replace} onReload={() => void load("none")}
                      onOpenAbility={(k) => openAbility(k)} />
            : <div className="kit-grid" aria-busy="true"><div className="kit-card kit-skeleton" /></div>
      ) : null}

      {tab === "library" && initial ? (
        <Library projectId={projectId} actor={actor} kit={kit} crew={crew} initial={initial}
                 onKitChanged={() => void load("none")} />
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
