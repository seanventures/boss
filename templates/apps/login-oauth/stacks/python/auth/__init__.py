"""login-oauth for a FastAPI app: Google, GitHub and BOSS (OIDC) sign-in with Authlib."""
from .config import load_config
from .routes import create_auth

__all__ = ["create_auth", "load_config"]
