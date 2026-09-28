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

## Separate Goldeneye host-effect path

`POST /v1/goldeneye/packages/install` calls
`goldeneye_paas.install_package`. This was a separate SSH path from workload
deployment. Community, OSS, local, and unknown editions now reject real host
installation before an install row, receipt, or SSH call. Simulated package
installation remains available. A governed ChangeSet, approval, budget, and
ResourceReceipt path is still required to offer real Goldeneye host changes in
Community. The non-Community real path remains a separate qualification item.

The static `api/openapi.community.yaml:5873` still describes the code-task SSH
deploy route. Regenerate and review Community OpenAPI from the gated API at the
final export rather than manually changing generated route descriptions here.
