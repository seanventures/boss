from __future__ import annotations

from fastapi import APIRouter

from .route_ownership import ENTERPRISE_EDITIONS, is_enterprise_path

enterprise_router = APIRouter()

__all__ = ["ENTERPRISE_EDITIONS", "enterprise_router", "is_enterprise_path"]
