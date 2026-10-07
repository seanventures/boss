import { githubWebOrigins } from "../../lib/identity/github-manifest";
import { GitHubAppHandoff } from "./handoff";

export const dynamic = "force-dynamic";
export const metadata = { title: "Opening GitHub · BOSS" };

/** The GitHub App manifest hand-off (lib/identity/github-manifest.ts): the only page whose CSP lets a
 *  form go to GitHub. */
export default function GitHubAppPage() {
  return <GitHubAppHandoff origins={githubWebOrigins()} />;
}
