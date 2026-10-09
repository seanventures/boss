import { pageAllowed } from "../../../components/identity/route-shell";
import { MY_PROJECTS_PATH, type MyProject } from "../../../lib/identity/held-anywhere";
import { serverGet } from "../../../lib/identity/server";
import { loadAuthOptions } from "../../(auth)/auth-options";
import { AppSignIn } from "./app-sign-in";

export const dynamic = "force-dynamic";

/** Identity › App sign-in (F6 §6.4): BOSS as the sign-in provider of the apps it builds. */
export default async function AppSignInPage() {
  if (!(await pageAllowed("/identity/app-sign-in"))) return null;
  const [projects, options] = await Promise.all([serverGet<{ projects: MyProject[] }>(MY_PROJECTS_PATH), loadAuthOptions()]);
  const mine = projects.ok
    ? projects.data.projects.filter((p) => p.permissions.includes("oidc_client:manage")).map((p) => ({ id: p.project_id, name: p.name ?? p.project_id }))
    : [];
  return <AppSignIn projects={mine} publicOrigin={options.public_origin ?? null} />;
}
