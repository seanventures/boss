import { NextResponse } from "next/server";

import { client } from "../../../../lib/client";

export async function GET() {
  try {
    return NextResponse.json(await client.getSandboxHelpers());
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
