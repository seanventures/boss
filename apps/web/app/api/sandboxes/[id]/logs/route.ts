import { NextResponse } from "next/server";

import { client } from "../../../../../lib/client";

// Same-origin proxy so the browser never calls the control-plane API directly.
export async function GET(_req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  try {
    return NextResponse.json(await client.getSandboxLogs(id));
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
