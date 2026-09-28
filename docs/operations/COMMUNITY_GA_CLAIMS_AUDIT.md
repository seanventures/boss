# Community GA claims audit — Discovery and SSH workload delivery

This pass compares Community-facing copy with the fail-closed Discovery and
BOSS-initiated SSH workload gates. It does not qualify the runtime or the final
export. Recheck these claims against the exact release commit and public image.

| Surface | Corrected claim |
|---|---|
| Root `README.md` (opening; two quick starts) | Outcome-to-PR path no longer promises a new Discovery run; both starts explain the paused research path and readable saved dossiers. |
| Exported `README.community.md` | Names paused Discovery web research and BOSS-initiated SSH workload planning/deployment; distinguishes installer SSH and console tunnels. Its reviewed hash is updated in `scripts/community_export_manifest.json`. |
| `docs/community/FEATURES.md` and `docs/product/V1_FEATURE_LOCK.md` | Separate readable target metadata/history from unavailable SSH workload planning and actuation. |
| Console template sheet, workflow starter, Deployment Manager, onboarding help, and Community nav copy | Remove unconditional research/ship promises or show the Community pause where operators decide what to start. |

`apps/web/app/launch/page.tsx` makes no specific Discovery or SSH workload
deployment promise. Example workflows use their own research steps; the paused
Discovery pipeline is a separate path, so those examples were not relabeled.

## Separate host-effect risk for security review

`POST /v1/goldeneye/packages/install` in `apps/api/app/main.py:18195-18221`
calls `goldeneye_paas.install_package`. In
`engine/boss_engine/services/goldeneye_paas.py:431-432,789-798`, the
`BOSS_SSH_HOST_DEPLOY_ALLOWED` setting and a node `ssh_host` enable the real
path; `:868-873` calls `_actuate_remote_module_install`, which reaches
`SSHHostAdapter._run_remote` at `:477-485`. This is distinct from the SSH
workload deployment gate and needs its own lease, budget/effect, and authority
review. `docs/product/GOLDENEYE_PAAS.md` was left unchanged because its package
install behavior may still be live. Do not describe all BOSS host effects as
gated until this path is resolved.

The static `api/openapi.community.yaml:5873` still describes the code-task SSH
deploy route. Regenerate and review Community OpenAPI from the gated API at the
final export rather than manually changing generated route descriptions here.
