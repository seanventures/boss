/**
 * HOW TO SET UP EACH SIGN-IN PROVIDER: one table, read by the Add sheet's "How to set this up"
 * and written into docs/community/SIGN_IN_SETUP.md (between the sso-guides markers) by
 * tests/identity-sso-guides.test.ts, which fails if the two disagree. Run it with
 * SSO_GUIDES_WRITE=1 to rewrite the doc from this table.
 *
 * A step may say {redirect}: the sheet shows the copyable redirect URL (or SAML's ACS and entity
 * ID) right there; the doc shows the URL's shape.
 */
import { newOAuthAppUrl } from "../github-urls";
export type GuideKind = "google" | "github" | "microsoft" | "okta" | "keycloak" | "generic" | "saml" | "apple";

export type Guide = {
  title: string;
  /** The heading's anchor in docs/community/SIGN_IN_SETUP.md. */
  anchor: string;
  steps: { text: string; link?: { label: string; href: string } }[];
  /** Exactly what goes back into BOSS's form. */
  copyBack: string[];
  /** The one thing that trips people up. */
  gotcha: string;
};

export const GUIDES: Record<GuideKind, Guide> = {
  google: {
    title: "Google", anchor: "google",
    steps: [
      { text: "In Google Cloud, create an OAuth client of type Web application.", link: { label: "Create a client", href: "https://console.cloud.google.com/auth/clients/create" } },
      { text: "Under Authorized redirect URIs, add {redirect}." },
      { text: "Under Audience, add yourself (and anyone testing) as a Test user.", link: { label: "Audience", href: "https://console.cloud.google.com/auth/audience" } },
      { text: "Copy the Client ID and Client secret into BOSS, then Add." },
    ],
    copyBack: ["Client ID", "Client secret"],
    gotcha: "While the app's publishing status is Testing, only listed Test users can sign in.",
  },
  github: {
    title: "GitHub", anchor: "github",
    steps: [
      { text: "On GitHub, register a new OAuth app (under an organization's settings for a team).", link: { label: "New OAuth app", href: newOAuthAppUrl() } },
      { text: "Set Authorization callback URL to {redirect}." },
      { text: "Generate a client secret." },
      { text: "Copy the Client ID and the secret into BOSS, then Add." },
    ],
    copyBack: ["Client ID", "Client secret"],
    gotcha: "A GitHub OAuth app has one callback URL: use one app per BOSS install.",
  },
  microsoft: {
    title: "Microsoft Entra ID", anchor: "microsoft-entra-id",
    steps: [
      { text: "In Entra, open App registrations and choose New registration.", link: { label: "App registrations", href: "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade" } },
      { text: "Pick Web as the platform and set the redirect URI to {redirect}." },
      { text: "Under Certificates & secrets, add a client secret and copy its Value." },
      { text: "Copy the Directory (tenant) ID, the Application (client) ID and the secret into BOSS, then Add." },
    ],
    copyBack: ["Tenant ID", "Client ID", "Client secret"],
    gotcha: "Use your Directory (tenant) ID, not \"common\": BOSS signs in one tenant's people.",
  },
  okta: {
    title: "Okta", anchor: "okta",
    steps: [
      { text: "In the Okta Admin Console, go to Applications › Create App Integration › OIDC › Web Application." },
      { text: "Set Sign-in redirect URI to {redirect}." },
      { text: "Assign the people or groups who may sign in." },
      { text: "Copy your Okta URL (the issuer), the Client ID and Client secret into BOSS, then Add." },
    ],
    copyBack: ["Issuer URL", "Client ID", "Client secret"],
    gotcha: "Only people assigned to the app can sign in.",
  },
  keycloak: {
    title: "Keycloak", anchor: "keycloak",
    steps: [
      { text: "In your realm's admin console, go to Clients › Create client (OpenID Connect)." },
      { text: "Turn on Client authentication, and set Valid redirect URIs to {redirect}." },
      { text: "Copy the client secret from the Credentials tab." },
      { text: "Copy the realm URL (the issuer), the Client ID and secret into BOSS, then Add." },
    ],
    copyBack: ["Issuer URL", "Client ID", "Client secret"],
    gotcha: "The issuer is the realm URL, ending in /realms/<name>.",
  },
  generic: {
    title: "Other OIDC", anchor: "other-oidc",
    steps: [
      { text: "In your provider, create a confidential web client with the authorization code flow." },
      { text: "Set its redirect URL to {redirect}, with scopes openid email profile." },
      { text: "Copy the issuer URL, Client ID and Client secret into BOSS, then Add." },
    ],
    copyBack: ["Issuer URL", "Client ID", "Client secret"],
    gotcha: "The issuer must serve /.well-known/openid-configuration over https.",
  },
  saml: {
    title: "SAML", anchor: "saml",
    steps: [
      { text: "In your identity provider, create a SAML 2.0 application for BOSS." },
      { text: "Give it {redirect}." },
      { text: "Turn on signed assertions, and send the email as the NameID or an email attribute." },
      { text: "Copy its entity ID, sign-in (SSO) URL and signing certificate into BOSS, then Add." },
    ],
    copyBack: ["IdP entity ID", "SSO URL", "Signing certificate"],
    gotcha: "BOSS refuses unsigned assertions: sign the assertion, not only the response.",
  },
  apple: {
    title: "Apple", anchor: "apple",
    steps: [
      { text: "In Apple Developer, create a Services ID with Sign in with Apple turned on.", link: { label: "Identifiers", href: "https://developer.apple.com/account/resources/identifiers/list/serviceId" } },
      { text: "Under its configuration, add your domain and the return URL {redirect}." },
      { text: "Create a Sign in with Apple key and note its Key ID and your Team ID." },
      { text: "Copy the Services ID (the client ID) and the client secret made from that key into BOSS, then Add." },
    ],
    copyBack: ["Services ID", "Client secret"],
    gotcha: "Apple posts back to BOSS (form_post), and needs a real domain, not an IP address.",
  },
};

/** The doc's "redirect" wording, where the sheet shows a copyable value. */
const DOC_REDIRECT: Record<"oidc" | "saml", string> = {
  oidc: "the redirect URL BOSS shows (`https://<your BOSS>/v1/auth/sso/<provider id>/callback`)",
  saml: "BOSS's ACS URL (`https://<your BOSS>/saml/v2/acs`) and entity ID (`https://<your BOSS>/saml/v2/metadata`)",
};

/** The guides as Markdown, for docs/community/SIGN_IN_SETUP.md. */
export function guidesMarkdown(): string {
  const out: string[] = [];
  for (const g of Object.values(GUIDES)) {
    out.push(`#### ${g.title}`, "");
    g.steps.forEach((s, i) => {
      const text = s.text.replace("{redirect}", DOC_REDIRECT[g.anchor === "saml" ? "saml" : "oidc"]);
      out.push(`${i + 1}. ${text}${s.link ? ` [${s.link.label}](${s.link.href})` : ""}`);
    });
    out.push("", `Copy back into BOSS: ${g.copyBack.join(", ")}.`, "", `Watch out: ${g.gotcha}`, "");
  }
  return out.join("\n").trimEnd() + "\n";
}

export const DOC_START = "<!-- sso-guides:start (generated from apps/web/lib/identity/sso-guides.ts) -->";
export const DOC_END = "<!-- sso-guides:end -->";

/** The published guide for a provider kind. */
export const SIGN_IN_DOC = "https://boss.sean.network/docs/SIGN_IN_SETUP/";
