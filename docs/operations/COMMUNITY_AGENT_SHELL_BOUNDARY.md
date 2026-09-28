# Community legacy agent shell boundary

The legacy unbounded agent loop no longer offers `run_shell` in Community, OSS, local, or
unrecognized editions. Direct calls to its handler return a denial before BOSS clones a
workspace or starts a shell. Repository read tools remain available within the configured
`BOSS_AGENT_REPO_ROOT`.
Those readers now exclude credential config paths such as `.npmrc`, `.pypirc`, `.netrc`,
`.docker/`, and SSH key files before returning file contents or names. Ordinary source reads
remain available.

`run_shell` launches `sh -lc` from a disposable checkout. That checkout limits where ordinary
edits persist, but it does not isolate the process from the BOSS host/container filesystem or
the network. The command denylist and sanitized environment cannot provide that boundary.
Community shell access remains paused until a dedicated OS sandbox enforces filesystem and
network access. Paid editions retain the existing legacy behavior while that isolation work
is tracked separately.
