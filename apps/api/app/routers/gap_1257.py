"""Recorded CVEs and the SBOM (BOSSAPP-1257).

GET /v1/gap-1257/cve-sbom?project_id=

Reads the scans already stored. It does not start a scanner.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from boss_engine.identity import P, Scope, permit

router = APIRouter(prefix="/v1/gap-1257", tags=["cve-sbom"])


@router.get("/cve-sbom")
@permit(P.ASSURANCE_READ, scope=Scope.project(query="project_id"))
def cve_sbom(project_id: str = Query(...)) -> dict:
    from apps.api.app.main import require_project_reader
    from boss_engine.config import get_settings
    from boss_engine.db import begin, get_engine
    from boss_engine.services.cve_sbom_screen import read_screen

    with begin(get_engine(get_settings())) as connection:
        require_project_reader(connection, project_id)
        return read_screen(connection, project_id)
