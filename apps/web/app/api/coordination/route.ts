import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const projectId = new URL(request.url).searchParams.get("project_id")?.trim();
  if (!projectId) return NextResponse.json({ error: "missing project_id" }, { status: 400 });

  try {
    const upstream = await fetch(
      `${API_URL}/v1/coordination?project_id=${encodeURIComponent(projectId)}`,
      { headers: authHeaders(), cache: "no-store" },
    );
    return new NextResponse(upstream.body, {
      status: upstream.status,
      headers: {
        "content-type": upstream.headers.get("content-type") ?? "application/json",
        "cache-control": "no-store",
      },
    });
  } catch {
    return NextResponse.json({ error: "Coordination feed unavailable" }, { status: 502 });
  }
}
