import { BossApiError } from "@boss/api-client";
import { NextResponse } from "next/server";

import { client } from "../../../../lib/client";
import { explainRawSecret } from "../../../../lib/hardware";
import { rawRequestUrl } from "../../../../lib/raw-request-url";

export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ path?: string[] }> };

const SECRET_FIELDS = new Set(["secret", "token", "password"]);

function failure(error: unknown) {
  if (error instanceof BossApiError) {
    return NextResponse.json(
      { error: error.message },
      { status: error.status || 502 },
    );
  }
  return NextResponse.json(
    { error: error instanceof Error ? error.message : "The hardware request failed." },
    { status: 502 },
  );
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function rejectedSecretField(body: Record<string, unknown>): string | null {
  for (const key of Object.keys(body)) {
    if (SECRET_FIELDS.has(key.toLowerCase())) {
      return "Send secret_ref. Do not send the secret, token, or password.";
    }
  }
  return null;
}

export async function GET(req: Request, ctx: Ctx) {
  const path = ((await ctx.params).path ?? []).join("/");
  const url = rawRequestUrl(req);
  const projectId = url.searchParams.get("project_id")?.trim() ?? "";
  if (!projectId) {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }
  const placeId = url.searchParams.get("place_id")?.trim() || undefined;
  try {
    if (path === "places") {
      return NextResponse.json(await client.listHardwarePlaces(projectId));
    }
    if (path === "resolve") {
      return NextResponse.json(await client.resolveHardwarePlace(projectId, placeId));
    }
  } catch (error) {
    return failure(error);
  }
  return NextResponse.json({ error: "not found" }, { status: 404 });
}

export async function POST(req: Request, ctx: Ctx) {
  const path = ((await ctx.params).path ?? []).join("/");
  let body: Record<string, unknown>;
  try {
    body = await req.json() as Record<string, unknown>;
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const leaked = rejectedSecretField(body);
  if (leaked) return NextResponse.json({ error: leaked }, { status: 400 });
  const projectId = text(body.project_id);
  const actor = text(body.actor);
  if (!projectId || !actor) {
    return NextResponse.json({ error: "project_id and actor are required" }, { status: 400 });
  }
  try {
    if (path === "places/local") {
      const hostname = text(body.hostname);
      const arch = text(body.arch);
      const osName = text(body.os);
      if (!hostname || !arch || !osName) {
        return NextResponse.json({ error: "hostname, arch, and os are required" }, { status: 400 });
      }
      return NextResponse.json(await client.registerLocalHardwarePlace({
        project_id: projectId,
        actor,
        hostname,
        arch,
        os: osName,
      }));
    }
    if (path === "places/agents") {
      const name = text(body.name);
      if (!name) return NextResponse.json({ error: "name is required" }, { status: 400 });
      return NextResponse.json(await client.enrollHardwareAgent({
        project_id: projectId,
        actor,
        name,
      }));
    }
    if (path === "places/providers") {
      const name = text(body.name);
      const providerKey = text(body.provider);
      const secretRef = text(body.secret_ref);
      if (!name || !providerKey || !secretRef) {
        return NextResponse.json(
          { error: "name, provider, and secret_ref are required" },
          { status: 400 },
        );
      }
      if (!["aws", "digitalocean", "generic"].includes(providerKey)) {
        return NextResponse.json(
          { error: "provider must be aws, digitalocean, or generic" },
          { status: 400 },
        );
      }
      const raw = explainRawSecret(secretRef);
      if (raw) return NextResponse.json({ error: raw }, { status: 400 });
      return NextResponse.json(await client.registerHardwareProvider({
        project_id: projectId,
        actor,
        name,
        provider: providerKey,
        secret_ref: secretRef,
      }));
    }
  } catch (error) {
    return failure(error);
  }
  return NextResponse.json({ error: "not found" }, { status: 404 });
}
