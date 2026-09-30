import { NextResponse } from "next/server";

import { client } from "../../../../../lib/client";

export async function GET(req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const path = new URL(req.url).searchParams.get("path") || "";
  try {
    return NextResponse.json(await client.getSandboxFile(id, path));
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
