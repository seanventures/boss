"""THE SECURITY SCAN ENGINE (APP_TEMPLATES_AND_SECURITY_LOOP.md §4, feature F4; stream S4).

Real tools, run in the build sandbox, against the project's OWN app only:

* `runners`      Semgrep (SAST, vendored rule packs), OSV-Scanner (dependencies, online through
                 egress: D6), OWASP ZAP (DAST: baseline by default, active API scan only when
                 asked: D12) — as the run's sandbox uid, with timeouts that fail a tool, never
                 report "0 findings";
* `parsers`      SARIF / OSV JSON / ZAP JSON -> normalised `Finding`s, size-limited, untrusted;
* `fingerprints` sha256(tool | rule | normalised location | normalised snippet): survives line
                 moves (code locations are file + enclosing function, DAST locations are method +
                 path template + parameter);
* `redact`       evidence is redacted before it is stored or shown (identity.redaction plus the
                 scanner-specific shapes: cookies, hardcoded secret literals), capped at 4 KB;
* `scope`        what may be scanned: the app's own checkout and its in-sandbox run, or a preview
                 of the same project and app. Never a free URL;
* `service`      scans and findings in the database, triage with a reason and an audit event;
* `report`       the before/after delta: fixed, removed, moved, still open, new.
"""
