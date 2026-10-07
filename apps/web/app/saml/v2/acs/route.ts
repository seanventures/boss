import { API_URL } from "../../../../lib/client";
import { forwardProtocol } from "../../../../lib/identity/protocol-proxy";

/**
 * The SAML Assertion Consumer Service on the public origin: the IdP's form POST, forwarded with
 * the identity cookies and nothing else (lib/identity/protocol-proxy.ts). The one cross-site write
 * middleware.ts allows, and only here.
 */
export const dynamic = "force-dynamic";

export const POST = (req: Request) => forwardProtocol(req, { apiUrl: API_URL });
