"""Multi-repo apps (docs/product/MULTIREPO.md).

    registry       the org's repositories: what the GitHub App reaches, which BOSS may use (tick),
                   and whether BOSS can really read, write and open PRs there (verify, don't trust)
    project_repos  which allowed repositories a project uses, with a role hint; One repo / Several
    components     an app's components -> repositories, path, access, dependency order
    grants         the exact repositories a run may touch (= the app's), checked at every mint
    change_sets    one change across N repositories: lanes in dependency order, linked PRs, one
                   approval pinned to the head SHAs, merge as a set, deploy together

A single-repo app is the degenerate case: one implicit component, a one-PR change set, and the
pipeline it always had.
"""
