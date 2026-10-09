# Banking starter (training variant): security note

**This template is insecure on purpose.** It is the banking starter with planted flaws, in the
style of OWASP WebGoat and Juice Shop, for learning to find and fix them with BOSS's security
review (Semgrep, the dependency audit, ZAP, the tester app and the review panel).

- It carries the label `insecure_by_design`. BOSS applies it only to a new lab project, shows a
  red "Insecure by design — training only" banner on its landing page and an
  `X-Insecure-By-Design` header on every response, keeps its preview private, and refuses to
  deploy it to a host.
- `vulns.yaml` is the answer key: each flaw's location, CWE and OWASP category, how a reviewer or
  scanner finds it, and the correct fix in the secure `banking-starter`.
- It protects nothing. Never put real users, real money or real passwords in it, and never copy
  its code into another app: use `banking-starter` instead.
- Its tests check that the bank works, not the flaws; detection is measured by BOSS's scanners
  against `vulns.yaml`.
