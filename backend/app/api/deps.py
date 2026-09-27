"""FastAPI dependencies: მომხმარებლის იდენტიფიკაცია და საერთო სერვისები.

ავთენტიფიკაცია (ვინ ხარ) -> OIDC JWT (prod) ან dev header-ები (მხოლოდ APP_ENV=dev).
ავტორიზაცია (რა შეგიძლია ამ კომპანიაში) -> ყოველთვის DB: tenant_members.role, RLS-ის ქვეშ.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status

from ..config import Settings, get_settings
from ..security.auth import AuthenticationError, OidcTokenVerifier

STAFF_ROLES = ("owner", "accountant")


@dataclass(frozen=True)
class Principal:
    tenant_id: UUID
    user_id: UUID
    role: str


def get_db(request: Request):
    return request.app.state.db


def get_router(request: Request):
    return request.app.state.llm_router


def get_secret_box(request: Request):
    return request.app.state.secret_box


def get_otp_broker(request: Request):
    return request.app.state.otp_broker


def get_token_verifier(request: Request) -> OidcTokenVerifier | None:
    return getattr(request.app.state, "token_verifier", None)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


async def _membership(db, tenant_id: UUID, *, user_id: UUID | None = None, subject: str | None = None) -> dict | None:
    """წევრობა მოწმდება მოთხოვნილი tenant-ის RLS კონტექსტში: სხვა კომპანიის წევრობას ვერც დავინახავთ."""
    column, value = ("u.id", user_id) if user_id is not None else ("u.auth_subject", subject)
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(
            f"SELECT m.user_id, m.role FROM tenant_members m JOIN users u ON u.id = m.user_id WHERE {column} = %s",
            (value,))
        return await cur.fetchone()


async def get_principal(
    settings: Settings = Depends(get_settings),
    db=Depends(get_db),
    verifier: OidcTokenVerifier | None = Depends(get_token_verifier),
    authorization: str | None = Header(default=None),
    x_tenant_id: UUID | None = Header(default=None),
    x_user_id: UUID | None = Header(default=None),
) -> Principal:
    if x_tenant_id is None:
        raise _unauthorized("X-Tenant-Id header is required")

    if settings.auth_mode == "oidc":
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip() or verifier is None:
            raise _unauthorized("bearer token is required")
        try:
            verified = await asyncio.to_thread(verifier.verify, token.strip())
        except AuthenticationError:
            raise _unauthorized("invalid or expired token") from None
        member = await _membership(db, x_tenant_id, subject=verified.subject)
        if (member is not None and member["role"] in STAFF_ROLES and settings.oidc_require_mfa_for_staff
                and not verified.has_mfa(settings.oidc_mfa_acr_values)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "multi-factor authentication is required for this role")
    else:
        # ⚠️ DEV ONLY: იდენტობა header-იდან (ყალბდება ტრივიალურად). check_startup_settings prod-ში ამას კრძალავს;
        # აქაც ვამოწმებთ, რათა შემთხვევით არასწორ კონფიგურაციაზე მოთხოვნა არ გაიაროს.
        if settings.env != "dev":
            raise _unauthorized("authentication is not configured")
        if x_user_id is None:
            raise _unauthorized("X-Tenant-Id and X-User-Id headers are required in dev")
        member = await _membership(db, x_tenant_id, user_id=x_user_id)

    if member is None:  # tenant არ არსებობს ან მომხმარებელი მისი წევრი არ არის - განსხვავებას არ ვამჟღავნებთ
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this organization")
    return Principal(tenant_id=x_tenant_id, user_id=member["user_id"], role=member["role"])


def require_role(*roles: str):
    async def checker(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
        return principal

    return checker
