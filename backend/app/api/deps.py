"""FastAPI dependencies: მომხმარებლის იდენტიფიკაცია და საერთო სერვისები.

ავთენტიფიკაციის რეჟიმები (APP_AUTH_MODE):
* oidc - `Authorization: Bearer <JWT>` + `X-Tenant-Id`. JWT მოწმდება JWKS-ით, როლი იკითხება
         tenant_members-იდან (RLS-ის ქვეშ), owner/accountant-ს სჭირდება MFA (`amr`).
* dev  - მხოლოდ APP_ENV=dev: `X-Tenant-Id`, `X-User-Id` და არასავალდებულო `X-Role` header-ები.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status

from ..config import Settings, get_settings
from ..security.oidc import AuthError, OidcVerifier

ROLES = ("owner", "accountant", "viewer")
_BEARER_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="ai-accountant"'}


@dataclass(frozen=True)
class Principal:
    tenant_id: UUID
    user_id: UUID
    role: str


@dataclass(frozen=True)
class Membership:
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


def get_oidc_verifier(request: Request) -> OidcVerifier | None:
    return getattr(request.app.state, "oidc", None)


def client_ip(request: Request) -> str | None:
    """აუდიტისთვის. reverse proxy-ის უკან uvicorn გაუშვით --proxy-headers --forwarded-allow-ips=<proxy>."""
    host = request.client.host if request.client else None
    try:
        return str(ip_address(host)) if host else None
    except ValueError:  # unix socket, "testclient" და ა.შ. - DB-ის inet სვეტში არ ჩაიწერება
        return None


async def find_membership(db, tenant_id: UUID, subject: str) -> Membership | None:
    async with db.tenant_tx(tenant_id) as conn:
        row = await (await conn.execute(
            "SELECT u.id AS user_id, m.role::text AS role FROM tenant_members m JOIN users u ON u.id = m.user_id"
            " WHERE u.auth_subject = %s", (subject,))).fetchone()
    return Membership(row["user_id"], row["role"]) if row else None


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, message, headers=_BEARER_CHALLENGE)


async def get_principal(
    settings: Settings = Depends(get_settings),
    verifier: OidcVerifier | None = Depends(get_oidc_verifier),
    db=Depends(get_db),
    authorization: str | None = Header(default=None),
    x_tenant_id: UUID | None = Header(default=None),
    x_user_id: UUID | None = Header(default=None),
    x_role: str | None = Header(default=None),
) -> Principal:
    if settings.auth_mode == "oidc":
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise _unauthorized("bearer token required")
        if verifier is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authentication is not initialised")
        try:
            claims = await verifier.verify(token.strip())
        except AuthError as exc:
            if exc.status == 401:
                raise _unauthorized(str(exc)) from None
            raise HTTPException(exc.status, str(exc)) from None
        if x_tenant_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-Tenant-Id header is required")
        member = await find_membership(db, x_tenant_id, claims.subject)
        if member is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this organisation")
        if member.role in settings.oidc_mfa_roles and not set(claims.amr) & set(settings.oidc_mfa_amr_values):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "multi-factor authentication is required for this role")
        return Principal(tenant_id=x_tenant_id, user_id=member.user_id, role=member.role)

    # ⚠️ DEV ONLY. სხვა გარემოში dev რეჟიმი აპლიკაციის გაშვებასაც ბლოკავს (main.lifespan).
    if settings.env != "dev":
        raise _unauthorized("authentication is not configured")
    if x_tenant_id is None or x_user_id is None:
        raise _unauthorized("X-Tenant-Id and X-User-Id headers are required in dev")
    role = x_role or "owner"
    if role not in ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"X-Role must be one of {ROLES}")
    return Principal(tenant_id=x_tenant_id, user_id=x_user_id, role=role)


def require_role(*roles: str):
    async def checker(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
        return principal

    return checker
