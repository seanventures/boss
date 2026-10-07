// @vitest-environment happy-dom
/**
 * NO IDENTITY SCREEN SHOWS A COMMUNITY USER A MACHINE VALUE.
 *
 * Every Identity and My-account screen is rendered from the stub's fixture org, as an Owner and
 * as a Viewer (whose disabled controls carry screen-reader reasons), and its visible text is
 * scanned (tests/_helpers/identity-raw.ts): no permission key (the name is shown, the key is only
 * a tooltip), no API error code, no audit event name, no raw id, no "∩"/"union"/"jti"/"JWKS"/
 * "ceiling". A static sweep then pins the classes of the old leaks in source: a permission is
 * drawn only by <Perm> (components/identity/kit.tsx), and no JSX prints an API `.code`,
 * `.error` or `.event_type` as text.
 */
import React, { act } from "react";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { AccessDenied } from "../components/identity/access-denied";
import { ErrorState } from "../components/identity/states";
import { Overview } from "../app/identity/overview";
import { PeopleList } from "../app/identity/people/people-list";
import { PersonProfile } from "../app/identity/people/[id]/person";
import { PERSON_TABS } from "../app/identity/people/[id]/tabs";
import { GroupsList } from "../app/identity/groups/groups-list";
import { GroupDetail } from "../app/identity/groups/[id]/group";
import { RoleList } from "../app/identity/roles/role-list";
import { RoleEditor } from "../app/identity/roles/[id]/role-editor";
import { ServiceAccounts } from "../app/identity/service-accounts/service-accounts";
import { AgentsScreen } from "../app/identity/agents/agents-screen";
import { PassportScreen } from "../app/identity/agents/[id]/passport";
import { DelegationView } from "../app/identity/agents/delegations/[id]/delegation";
import { AuditScreen } from "../app/identity/audit/audit-screen";
import { EMPTY_FILTERS } from "../app/identity/audit/audit-query";
import { SignInSettings } from "../app/identity/sign-in/sign-in-settings";
import { MyKeys } from "../app/account/keys/my-keys";
import { AccountSecurity } from "../app/account/security";
import { AgentsForMe } from "../app/account/agents/agents-for-me";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import * as F from "../lib/identity/stub/fixtures";
import { resetStub, stubHandle } from "../lib/identity/stub/server";
import type { AuditPage, DelegationDetail, EffectiveAccess, Passport, Posture, Provider, SecurityPolicy } from "../lib/identity/types";
import { rawLeaks } from "./_helpers/identity-raw";
import { req, signIn } from "./_helpers/identity-stub-session";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NOW = Date.now();
let root: Root | null = null;
let host: HTMLElement | null = null;

function as(email: string) {
  const s = signIn(email);
  const get = <T,>(path: string) => stubHandle(req("GET", path, { cookies: s.cookies }), NOW).body as T;
  return { s, me: get<MeResponse>("/v1/identity/me"), org: get<MePermissionsResponse>("/v1/identity/me/permissions"), get };
}

beforeEach(() => resetStub(NOW));
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  vi.unstubAllGlobals();
});

async function mount(who: ReturnType<typeof as>, node: React.ReactElement): Promise<HTMLElement> {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://x");
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
    const out = stubHandle({ method: (init?.method ?? "GET").toUpperCase(), path: url.pathname, query: url.searchParams, body,
      cookies: who.s.cookies, headers: new Headers({ "x-boss-csrf": who.s.csrf }) });
    return new Response(JSON.stringify(out.body), { status: out.status, headers: { "content-type": "application/json" } });
  }));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<IdentityProvider me={who.me} org={who.org}><NowProvider now={NOW}>{node}</NowProvider></IdentityProvider>));
  for (let i = 0; i < 5; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return host;
}

function screens(who: ReturnType<typeof as>): [string, React.ReactElement][] {
  const people = F.people(NOW);
  const roles = F.roles(NOW);
  const groups = F.groups(NOW, people);
  const sam = people.find((p) => p.id === "usr_sam")!;
  // The data is read as the Owner (a Viewer's reads are refused); the screen renders as `who`.
  const owner = as("param@example.com");
  const eff = owner.get<EffectiveAccess>("/v1/identity/principals/usr_sam/effective");
  const audit = owner.get<AuditPage>("/v1/identity/audit");
  const passport = stubHandle(req("GET", "/v1/identity/agents/agt_forge/passport", { cookies: signIn("param@example.com").cookies }), NOW).body as Passport;
  const grant = stubHandle(req("GET", "/v1/identity/delegations/dlg_01JB7Q", { cookies: signIn("param@example.com").cookies }), NOW).body as DelegationDetail;
  const providers = F.providers(NOW) as unknown as Provider[];
  const policy = F.policy() as unknown as SecurityPolicy;
  const posture = stubHandle(req("GET", "/v1/identity/posture", { cookies: signIn("param@example.com").cookies }), NOW).body as Posture;
  const out: [string, React.ReactElement][] = [
    ["overview", <Overview key="o" posture={posture} people={people} roles={roles} groups={groups} serviceAccounts={F.serviceAccounts(NOW)}
      agents={F.passports(NOW)} events={F.auditEvents(NOW).slice(0, 6) as never} />],
    ["people", <PeopleList key="p" people={people} roles={roles} groups={groups} />],
    ["groups", <GroupsList key="g" groups={groups} />],
    ...groups.map((g): [string, React.ReactElement] => [`group ${g.id}`, <GroupDetail key={g.id} group={g} people={people} roles={roles} />]),
    ["roles", <RoleList key="r" roles={roles} />],
    ...roles.map((r): [string, React.ReactElement] => [`role ${r.key}`, <RoleEditor key={r.id} role={r} versions={F.roleVersions(NOW)} roles={roles} />]),
    ["service accounts", <ServiceAccounts key="sa" initial={F.serviceAccounts(NOW)} roles={roles} noExpiryAllowed={false} maxDays={365} selected="sva_ci_deployer" />],
    ["agents", <AgentsScreen key="a" agents={F.passports(NOW)} delegations={F.delegations(NOW)} />],
    ["passport", <PassportScreen key="pp" initial={passport} grants={F.delegations(NOW)} />],
    ["delegation", <DelegationView key="d" initial={grant} />],
    ["audit", <AuditScreen key="au" initial={audit} initialFilters={EMPTY_FILTERS} initialEvent={audit.events?.[0]?.id ?? null} />],
    ["sign-in", <SignInSettings key="si" providers={providers} policy={policy} legacy={posture.legacy} />],
    ["sign-in, none", <SignInSettings key="si0" providers={[]} policy={policy} legacy={[]} />],
    ["my keys", <MyKeys key="k" keys={F.personalKeys(NOW).usr_sam ?? []} />],
    ["my security", <AccountSecurity key="sec" me={who.me} factors={F.factors(NOW).usr_sam ?? []} identities={[]} host="boss.acme.dev" />],
    ["agents for me", <AgentsForMe key="afm" delegations={F.delegations(NOW).filter((d) => d.user.id === "usr_sam")} agents={null} connections={[]} untilRevokedAllowed={false} openNew={false} />],
    ["access denied", <AccessDenied key="ad" permission="audit:read" page="Audit" />],
    ["error state", <ErrorState key="es" what="people" failure={{ status: 503, code: "identity_unavailable" }} />],
    ["error state 403", <ErrorState key="es3" what="people" failure={{ status: 403, code: "forbidden", permission: "user:read" }} />],
  ];
  for (const t of PERSON_TABS) {
    out.push([`person ${t.id}`, <PersonProfile key={`pp-${t.id}`} person={sam} tab={t.id} factors={F.factors(NOW).usr_sam} sessions={F.sessions(NOW).usr_sam}
      effective={eff} projectEffective={[]} roles={roles} delegations={F.delegations(NOW).filter((d) => d.user.id === "usr_sam")}
      keys={F.personalKeys(NOW).usr_sam} events={F.auditEvents(NOW) as never} />]);
  }
  return out;
}

describe("no identity screen shows a raw permission key, error code, event name, id or jargon", () => {
  for (const [label, email] of [["an Owner", "param@example.com"], ["a Viewer", "diego@acme.dev"]] as const) {
    it(`as ${label}`, async () => {
      const who = as(email);
      const found: string[] = [];
      for (const [name, node] of screens(who)) {
        const el = await mount(who, node);
        for (const l of rawLeaks(el)) found.push(`${name}: ${l.kind} ${l.text}`);
        act(() => root?.unmount());
        host?.remove();
        root = null;
      }
      expect(found).toEqual([]);
    });
  }
});

describe("the classes of the old leaks stay closed in source", () => {
  const ROOTS = ["app/identity", "app/account", "app/(auth)", "components/identity"];
  const files = (dir: string): string[] => readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? files(p) : /\.tsx$/.test(n) ? [p] : [];
  });
  const all = ROOTS.flatMap((r) => files(join(process.cwd(), r)));

  it("a permission is never printed as a raw <code> chip (only <Perm> or a name)", () => {
    const bad = all.filter((f) => !f.endsWith("components/identity/kit.tsx"))
      .flatMap((f) => readFileSync(f, "utf8").split("\n").map((l, i) => [f, i + 1, l] as const))
      .filter(([, , l]) => /<code[^>]*\bidn-perm\b/.test(l))
      .map(([f, n]) => `${f.replace(process.cwd() + "/", "")}:${n}`);
    expect(bad).toEqual([]);
  });

  it("no JSX prints an API code, error or event type as text", () => {
    const bad = all
      .flatMap((f) => readFileSync(f, "utf8").split("\n").map((l, i) => [f, i + 1, l] as const))
      .filter(([, , l]) => !/data-raw-ok/.test(l))
      .filter(([, , l]) => /(>|·\s*)\{[\w.?]+\.(code|event_type)\}|>\{[\w.?]+\.error\}<|\$\{[\w.?]+\.(code|event_type)\}/.test(l)
        && !/title=|data-|aria-|key=|href=|className=/.test(l.split(/\{[\w.?]+\.(?:code|event_type|error)\}/)[0].slice(-40)))
      .map(([f, n, l]) => `${f.replace(process.cwd() + "/", "")}:${n} ${l.trim().slice(0, 100)}`);
    expect(bad).toEqual([]);
  });
});
