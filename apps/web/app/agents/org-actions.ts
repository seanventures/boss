/**
 * Organization's writes (org-view spec §4.6), each through the call the rest of the console
 * already makes, as the signed-in actor (never a literal), and each followed by a board refresh
 * past the server's cache. A failed write toasts the API's own sentence.
 */
import { kitApi } from "../../lib/kit-api";
import { updateAgent } from "../../lib/agents";
import { toast } from "../../lib/agent-world";
import { TEAM_LABELS } from "../../lib/agents-live-board";
import { nameOf } from "./org-shared";

export type OrgActions = {
  /** Give one ability to one agent: the Kit's holders, set semantics (current + this agent). */
  give: (ability: string, label: string, roleKey: string) => Promise<boolean>;
  /** The Kit's `Apply recommended`, for these agents only. */
  giveRecommended: (roles: string[]) => Promise<boolean>;
  move: (roleKey: string, team: string, teamLabel?: string) => Promise<boolean>;
};

export function orgActions(projectId: string, actor: string, refresh: () => void): OrgActions {
  return {
    async give(ability, label, roleKey) {
      const current = await kitApi.ability(projectId, ability);
      if (!current.ok) { toast("error", current.error); return false; }
      const holders = current.data.holders ?? [];
      const r = await kitApi.holders(projectId, actor, ability, [...new Set([...holders, roleKey])]);
      if (!r.ok) { toast("error", r.error); return false; }
      toast("success", `Gave ${label} to ${nameOf(roleKey)}`);
      refresh();
      return true;
    },
    async giveRecommended(roles) {
      const r = await kitApi.recommended(projectId, actor, roles);
      if (!r.ok) { toast("error", r.error); return false; }
      toast("success", `Gave ${r.data.granted.length} ${r.data.granted.length === 1 ? "ability" : "abilities"}`);
      refresh();
      return true;
    },
    async move(roleKey, team, teamLabel) {
      const r = await updateAgent(projectId, actor, roleKey, { team });
      if (!r.ok) { toast("error", r.error); return false; }
      toast("success", `Moved to ${TEAM_LABELS[team]?.label ?? teamLabel ?? team}`);
      refresh();
      return true;
    },
  };
}
