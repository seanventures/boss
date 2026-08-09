# BOSS Agent Toolchain Intake

Status: Implemented Community control-plane foundation
Plane: BOSS-333
Last updated: August 8, 2026

## Purpose

Third-party prompts, agents, skills, hooks, MCP configurations, commands, and
installers are software-supply-chain inputs. BOSS can learn from them without
granting them instruction authority or executing them. The intake path records
an immutable repository URL and full Git revision, verifies license posture,
hashes a bounded text snapshot, classifies candidates, stores only normalized
metadata and findings, and requires independent review before a native BOSS
implementation can be activated.

The registry never clones a repository, runs an installer, invokes a package
lifecycle script, activates a hook or MCP server, or stores raw upstream source.
Fetching is an explicit adapter concern outside this service. An upstream
candidate is inspiration or review input—not a runtime capability.

## Audited sources

| Source | Pinned revision | License posture | BOSS disposition |
| --- | --- | --- | --- |
| `thedotmack/claude-mem` | `4702c337d85aa12e8ab7f845264a78885676261f` | Apache-2.0 | Pattern-only: progressive disclosure and durable memory ideas feed the BOSS Knowledge Plane. |
| `affaan-m/everything-claude-code` | `51a6950bde756fe3ebc8879aa0c8ee49b9c53e78` | MIT | Selective, quarantined candidate assessment; no automatic agent/skill/hook activation. |
| `rohitg00/awesome-claude-code-toolkit` | `ebdf1d596d2cde5c5cceb32177e8d1cf4829e7d9` | Apache-2.0 | Selective, quarantined candidate assessment; linked projects retain their own licenses. |
| `azrlb/claude-code-brownfield-toolkit` | `53b24832cfccb7c372779af0d35e567481002dbb` | MIT | Pattern-only: native architecture and edit preflight gates. |
| `Dicklesworthstone/destructive_command_guard` | `17af4985debfc9729e43cb7d1a285a2b848170c3` | Restricted rider | Blocked. No code, rules, or derived content is imported. BOSS's command guard is independently authored. |
| `JuliusBrussee/caveman` | `11ddc0c9813c8f75365cd5be2f753df08712f154` | MIT | Pattern-only, opt-in terse output; never compress policy, proof, approval, contracts, or audit evidence. |
| `ComposioHQ/awesome-claude-skills` | `be2a406907dbc61b73e6827ded415c96139d13a2` | Per-item/mixed | Catalog metadata only until an individual item's license and security posture are proven. |

This is an engineering disposition, not legal advice. The manifest at
`packages/specpack/toolchains/upstreams.yaml` is the runtime source of the
pinned identities and dispositions.

## Intake state and guarantees

`ToolchainSource` records provenance, license status, integration mode, and
adopted BOSS-native components. `ToolchainImportRun` records a snapshot hash,
revision, file/byte inventory, findings, and disposition. `ToolchainCandidate`
records only path, kind, content hash, normalized contract, risk, and review
state.

Assessment is bounded to 500 UTF-8 text files, 256 KiB per file, and 2 MiB per
snapshot. Path escape, revision drift, destructive instructions, remote script
pipes, policy-bypass language, executable hooks/scripts/packages, and
restricted licenses fail closed. Dynamic execution and secret access require
review. Mixed or missing repository-wide licensing remains catalog-only.

Approval does not execute or install a candidate. It permits an independently
authored native implementation to proceed through normal SkillSpec,
Integration, policy, tests, lease, budget, approval, and Receipt gates.

## Native runtime tools

- `toolchain.source.assess` performs the non-executing snapshot assessment.
- `toolchain.command.guard` returns deterministic `allow`, `review`, or `block`
  decisions and a command hash. A model cannot override a block.
- `toolchain.context.pack` deduplicates and orders context under an explicit
  input budget. Policy, approval, contract, proof, or explicitly required
  chunks are never truncated; if they do not fit, the pack operation blocks.
  Optional omissions and duplicates are returned and audited.

API surfaces are `/v1/toolchain/sources`, `/v1/toolchain/imports`,
`/v1/toolchain/imports/assess`,
`/v1/toolchain/candidates/{candidate_id}/review`,
`/v1/toolchain/commands/evaluate`, and `/v1/toolchain/context/pack`.

## Security and operations

- Raw source never enters Postgres, events, prompts, or API read models.
- Raw command text is not emitted to events; only its hash and matched rule IDs
  are persisted.
- Context audit events contain identifiers and token counts, not chunk text.
- Source imports require `pack_manage`; direct command and context operations
  require `command_submit`; SkillRuntime adds its own caller and lease gates.
- External effects remain outside this registry and still require their normal
  adapter, budget, approval, dedupe, and Receipt contracts.
