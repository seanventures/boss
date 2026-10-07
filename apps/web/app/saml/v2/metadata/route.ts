import { API_URL } from "../../../../lib/client";
import { forwardProtocol } from "../../../../lib/identity/protocol-proxy";

/** The SP metadata on the public origin, from the API (lib/identity/protocol-proxy.ts). */
export const dynamic = "force-dynamic";

const handler = (req: Request) => forwardProtocol(req, { apiUrl: API_URL });
export const GET = handler;
export const HEAD = handler;
