"""The Project experience (BOSSAPP-913 / 914): one read per Project-page tab, the captain, the
onboarding agent and the jump search.

Contract: docs/product/PROJECT_EXPERIENCE_CONTRACT.md. Every module here READS what BOSS already
records (workflow runs, code tasks, decisions, deployments, alerts, the dossier, the ambient
board) and shapes it for a diagram-first page. The only new state is the captain choice and the
onboarding run's record; every write goes through a path that already exists.
"""
