# plain-node

The plumbing gate's second golden fixture (tests/e2e/gate/plumbing.py): a plain Node ESM app in
`plain/` with no build, typecheck or lint script and its own tests under `node --test`. BOSS's web
gate must pass it by running what it defines: the install, then `npm test`
(crun_decc93dbb0154e768871 gated a Node app like this on `npm run build` and never ran its tests).
