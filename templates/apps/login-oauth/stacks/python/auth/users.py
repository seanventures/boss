"""Users and their linked identities (schema.sql has the tables). This in-memory store is the
reference; back the same methods with the app's database when adapting the template."""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional


class AccountExistsError(Exception):
    code = "account_exists_unverified_email"


@dataclass
class User:
    id: str
    email: Optional[str]
    email_verified: bool
    name: Optional[str] = None
    avatar: Optional[str] = None
    providers: list = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class UserStore:
    def __init__(self, admin_emails: Iterable[str] = ()) -> None:
        self._users: dict[str, User] = {}
        self._identities: dict[tuple[str, str], str] = {}
        self._admins = {e.lower() for e in admin_emails}
        self._lock = threading.Lock()

    def get(self, user_id: Optional[str]) -> Optional[User]:
        return self._users.get(user_id) if user_id else None

    def _by_email(self, email: Optional[str]) -> Optional[User]:
        return next((u for u in self._users.values() if email and u.email == email), None)

    def roles_for(self, user: User) -> list[str]:
        roles = ["user"]
        if user.email and user.email_verified and user.email in self._admins:
            roles.append("admin")
        return roles

    def sign_in(self, *, provider: str, subject: str, email: Optional[str], email_verified: bool,
                name: Optional[str] = None, avatar: Optional[str] = None) -> User:
        """Account linking is `verified_email_only`: a new identity joins an existing account with
        the same email ONLY when both the new provider and the existing account have that email
        verified. Otherwise anyone who can register the address, unverified, at some provider
        could take the account over (or pre-create it and wait for the owner to link into it)."""
        email = email.lower() if email else None
        with self._lock:
            user = self.get(self._identities.get((provider, subject)))
            if user is None:
                existing = self._by_email(email)
                if existing is not None:
                    if not (email_verified and existing.email_verified):
                        raise AccountExistsError()
                    user = existing
                else:
                    user = User(id=str(uuid.uuid4()), email=email, email_verified=bool(email_verified))
                    self._users[user.id] = user
                self._identities[(provider, subject)] = user.id
                if provider not in user.providers:
                    user.providers.append(provider)
            if name:
                user.name = name
            if avatar:
                user.avatar = avatar
            return user
