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
