import { NextResponse } from "next/server";

import { client } from "../../../../../lib/client";

export async function POST(req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const body = await req.json().catch(() => ({}));
  try {
    return NextResponse.json(
      await client.execSandbox(id, body.command || "", !!body.break_glass, body.reason),
    );
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
