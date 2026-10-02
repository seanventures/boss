/**
 * Client for BOSS first-class IdP product packs.
 * Catalog + dry-run blueprints over generic OIDC/SAML/SCIM runtime.
 * Never handles raw secrets — only secret_ref names.
 */

import { writeAwareFetch } from "./api-write";
import { API_URL, PROJECT_ID, authHeaders, resolveProjectId } from "./client";

export type IdpCatalogItem = {
  id: string;
  name: string;
  category: string;
  protocol_support: string[];
  primary_protocol: string;
  stars: number;
  market_notes: string;
  docs_url: string;
  status: "available" | "planned" | string;
};

export type IdpCatalogResponse = {
  version: number;
  product: string;
  count: number;
  items: IdpCatalogItem[];
  standards: string[];
  runtime: Record<string, string>;
};

export type IdpConfigField = {
  key: string;
  label: string;
  placeholder?: string;
  required?: boolean;
  secret?: boolean;
};

export type IdpDetail = {
  id: string;
  name: string;
  category: string;
  protocol_support: string[];
  primary_protocol: string;
  stars: number;
  market_notes: string;
  setup_steps: string[];
  default_oidc_claim_map: Record<string, string>;
  metadata_url_templates: Record<string, string>;
  docs_url: string;
  status: string;
  config_fields: IdpConfigField[];
  enterprise_provider_type_default: string;
};

export type IdpDetailResponse = {
  idp: IdpDetail;
  blueprint_template: {
    path: string;
    method: string;
    required_fields: string[];
    optional_fields: string[];
    example_body: Record<string, unknown>;
  };
};

export type IdpBlueprintRequest = {
  project_id: string;
  variables?: Record<string, string>;
  provider_type?: string;
  display_name?: string;
  audience?: string;
  secret_ref?: string;
  status?: string;
  actor?: string;
  apply?: boolean;
};

export type IdpBlueprintResponse = {
  dry_run: boolean;
  idp_id: string;
  idp_name: string;
  project_id: string;
  provider_type: string;
  setup_steps: string[];
  default_oidc_claim_map: Record<string, string>;
  rendered_endpoints: Record<string, string>;
  docs_url: string;
  enterprise_upsert_path: string;
  enterprise_payload: Record<string, unknown>;
  notes: string[];
};

export type IdpProjectStatusItem = {
  idp_id: string;
  name: string;
  catalog_status: string;
  configured: boolean;
  configurations: Array<{
    id?: string;
    provider_key?: string;
    provider_type?: string;
    display_name?: string;
    status?: string;
    test_status?: string;
    issuer?: string;
    blocking_gaps?: string[];
    secret_ref?: string;
    updated_at?: string;
  }>;
};

export type IdpProjectStatusResponse = {
  project_id: string;
  configured_count: number;
  catalog_count: number;
  items: IdpProjectStatusItem[];
};

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_URL}${path}`;
  const response = await writeAwareFetch(url, {
    ...init,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...authHeaders(
        init?.headers as Record<string, string> | undefined,
      ),
    },
    cache: "no-store",
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(
      `IdP API ${response.status} on ${path}: ${text.slice(0, 240)}`,
    );
  }
  return (await response.json()) as T;
}

export async function listIdentityProviderCatalog(): Promise<IdpCatalogResponse> {
  return apiFetch<IdpCatalogResponse>("/v1/identity-providers");
}

export async function getIdentityProviderDetail(
  idpId: string,
): Promise<IdpDetailResponse> {
  return apiFetch<IdpDetailResponse>(
    `/v1/identity-providers/${encodeURIComponent(idpId)}`,
  );
}

export async function materializeIdentityProviderBlueprint(
  idpId: string,
  body: IdpBlueprintRequest,
): Promise<IdpBlueprintResponse> {
  return apiFetch<IdpBlueprintResponse>(
    `/v1/identity-providers/${encodeURIComponent(idpId)}/blueprint`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

export async function fetchIdentityProviderProjectStatus(
  projectId: string = PROJECT_ID,
): Promise<IdpProjectStatusResponse> {
  const resolved = resolveProjectId(projectId);
  const qs = new URLSearchParams({ project_id: resolved });
  return apiFetch<IdpProjectStatusResponse>(
    `/v1/identity-providers/project-status?${qs.toString()}`,
  );
}

export type IdentityProvidersFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  catalog: IdpCatalogItem[];
  standards: string[];
  projectStatus: IdpProjectStatusItem[];
  configuredCount: number;
};

export async function fetchIdentityProvidersFeed(
  projectId: string = PROJECT_ID,
  active = true,
): Promise<IdentityProvidersFeed> {
  const resolved = resolveProjectId(projectId);
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return unfetchedIdentityProvidersFeed(resolved);
  try {
    const [catalog, status] = await Promise.all([
      listIdentityProviderCatalog(),
      fetchIdentityProviderProjectStatus(resolved),
    ]);
    return {
      projectId: resolved,
      apiReachable: true,
      catalog: catalog.items,
      standards: catalog.standards,
      projectStatus: status.items,
      configuredCount: status.configured_count,
    };
  } catch (error) {
    return unfetchedIdentityProvidersFeed(
      resolved,
      error instanceof Error ? error.message : "IdP API unavailable",
    );
  }
}

function unfetchedIdentityProvidersFeed(
  projectId: string,
  blockedReason?: string,
): IdentityProvidersFeed {
  return {
    projectId,
    apiReachable: false,
    blockedReason,
    catalog: [],
    standards: [],
    projectStatus: [],
    configuredCount: 0,
  };
}
