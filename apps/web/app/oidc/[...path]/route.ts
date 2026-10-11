import { API_URL } from "../../../lib/client";
import { forwardOidc } from "../../../lib/oidc-proxy";

/**
 * BOSS as an OIDC provider on the public origin (F6): the issuer's endpoints, forwarded to the
 * API with only what each protocol step carries (lib/oidc-proxy.ts).
 */
export const dynamic = "force-dynamic";

const handler = (req: Request) => forwardOidc(req, { apiUrl: API_URL });
export const GET = handler;
export const POST = handler;
