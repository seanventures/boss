import { API_URL } from "../../../../lib/client";
import { forwardProtocol } from "../../../../lib/identity/protocol-proxy";

/**
 * SCIM 2.0 on the public origin: the IdP's bearer token passes through; no session, no CSRF, no
 * cookie (lib/identity/protocol-proxy.ts). Server-to-server only.
 */
export const dynamic = "force-dynamic";

const handler = (req: Request) => forwardProtocol(req, { apiUrl: API_URL });
export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
