"""Minimal password auth using signed session cookies.

If APP_PASSWORD is unset, the dashboard is open (useful for local dev). When it
is set, every page except /login and static assets requires a valid session.
Passwords are compared in constant time and never logged or sent to the client.
"""
from __future__ import annotations

import hmac

from fastapi import Request

from .config import settings

SESSION_KEY = "authed"


def is_authenticated(request: Request) -> bool:
    if not settings.login_required:
        return True
    return bool(request.session.get(SESSION_KEY))


def check_password(candidate: str) -> bool:
    if not settings.login_required:
        return True
    return hmac.compare_digest(str(candidate or ""), settings.app_password)


def login(request: Request) -> None:
    request.session[SESSION_KEY] = True


def logout(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)
