"""The Acceptance Agent (epic BOSSAPP-1213): does the delivered work do what was asked?

`contract` — the frozen, versioned list of what was asked per build lineage (AA-A, BOSSAPP-1214).
`verdict`  — one verdict for both layers: parse, evidence, judge; command evidence verified
             against the session's record (AA-F, BOSSAPP-1220).
`session`  — the throwaway clone of the PR head, the command record, replay first (AA-E,
             BOSSAPP-1219); `probes` — the replayable proofs a check leaves behind.
`checks`   — every acceptance check as a row; `ledger` — the findings ledger: every gap and
             finding, deduplicated, fixed only on proof, follow-ups built from it (AA-B,
             BOSSAPP-1216).
`policy`   — per-profile policy (`lean` / `balanced` / `strict` / `vag`) and budget floor
             (AA-G, BOSSAPP-1222).
`report`   — merged run report v2, merge-card payload, ticket comment, refresh in place
             (AA-H, BOSSAPP-1223).
`filing`   — ticket filing + status sync + receipt-guarded comment on change (AA-J,
             BOSSAPP-1225).
"""
