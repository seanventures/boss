# Community API route boundary

`apps/api/app/routers/route_ownership.py` owns the path classification shared by
both editions. It is policy only and has no import from paid modules.

`apps/api/app/routers/community.py` accepts an optional paid router from the
composition root. Community startup omits that argument and drops paid route
definitions before mounting the Community router. A paid edition must supply
its router explicitly; missing injection raises before routes are changed.

This is one API startup seam, not a separable Community API yet. The legacy
`apps/api/app/main.py` still imports and instantiates paid services and defines
paid handlers alongside Community handlers. It remains excluded from the
Community export candidate until those dependencies and handler definitions
are moved behind a paid-only composition root. The export tool must continue
to fail closed while that split and its other reported blockers remain.

The SOC 2 collection endpoint is the first paid route moved behind that root.
Its service now lives under `apps/api/app/enterprise_extensions/`, outside the
Community engine. Community startup does not import that paid package; a paid
edition registers the endpoint in `routers/enterprise.py` before route
partitioning. The `audit_export` actor check and response remain the same. The
Community export manifest must exclude `apps/api/app/enterprise_extensions/`.

Hosted Account Connect and marketplace API routes are the next paid group in
that package. Their 16 route contracts and denial audit helpers are registered
only in paid editions. The separate Community `/v1/cloud/*` Easy Connect client
remains mounted, so a self-hosted operator can choose to link later without
making hosted Account Connect a startup dependency.

Enterprise cluster validation and support-case routes are also registered from
the paid package. The shared `EnterpriseService` remains in the main API because
Community self-hosted identity and SCIM use it; those Community routes must not
be removed with the paid cluster operations. Goldeneye PaaS remains Community.
