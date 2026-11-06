#!/usr/bin/env python3
"""Seed a HelpDesk AI Demo-like project for the Project experience (BOSSAPP-913 / 914).

NON-PRODUCTION ONLY. For the console engineer and the tester: one project with 3 merged PRs, a
running build, a PR to merge, a deploy to approve, a healthy staging deploy with named demo
users, two roadmap items and a dossier (apps/api/tests/project_experience_seed.py — the same
rows the API tests read).

    export DATABASE_URL=postgresql+psycopg://boss:…@localhost:5432/boss_dev
    PYTHONPATH=engine:apps/api:. python3 scripts/seed-project-experience.py [--slug helpdesk-ai-demo]

Refuses a database whose name does not say it is disposable (test, dev, demo, scratch, …), so
it can never write into a running install's database by accident.
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid

DISPOSABLE = ("test", "dev", "demo", "scratch", "tmp", "ci", "local")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slug", default=f"helpdesk-ai-demo-{uuid.uuid4().hex[:4]}")
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL", "")
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower() if "/" in url else ""
    if not name or not any(word in name for word in DISPOSABLE):
        print(f"Refusing: database {name or '(none)'!r} does not look disposable "
              f"(its name must contain one of {', '.join(DISPOSABLE)}).", file=sys.stderr)
        return 2

    from apps.api.tests.project_experience_seed import seed
    from boss_engine.config import get_settings
    from boss_engine.db import ensure_schema, get_engine
    from boss_engine.services import projects

    settings = get_settings()
    engine = get_engine(settings)
    ensure_schema(engine)
    with engine.begin() as db:
        made = projects.create(db, settings, auth=None, name="HelpDesk AI Demo", slug=args.slug,
                               actor="seed")
        story = seed(db, made["project_id"])
    print(f"Seeded {story['project_id']}: open ?project_id={story['project_id']}&area=project")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
