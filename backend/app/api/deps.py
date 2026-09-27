"""FastAPI dependencies: მომხმარებლის იდენტიფიკაცია და საერთო სერვისები."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status

from ..config import Settings, get_settings


@dataclass(frozen=True)
class Principal:
    tenant_id: UUID
    user_id: UUID
    role: str


async def get_principal(
    settings: Settings = Depends(get_settings),
    x_tenant_id: UUID | None = Header(default=None),
    x_user_id: UUID | None = Header(default=None),
) -> Principal:
    """⚠️ DEV ONLY: tenant/user header-ებიდან.

    production-ში აქ უნდა იყოს OIDC JWT ვალიდაცია (Keycloak / Auth0 / Cognito): ხელმოწერა, aud, exp,
    და tenant_members-იდან როლის წაკითხვა. ამ ჩანაცვლებამდე prod რეჟიმში ყველა მოთხოვნა უარყოფილია.
    """
    if settings.env != "dev":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication is not configured")
    if x_tenant_id is None or x_user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "X-Tenant-Id and X-User-Id headers are required in dev")
    return Principal(tenant_id=x_tenant_id, user_id=x_user_id, role="owner")


def require_role(*roles: str):
    async def checker(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
        return principal

    return checker


def get_db(request: Request):
    return request.app.state.db


def get_router(request: Request):
    return request.app.state.llm_router


def get_secret_box(request: Request):
    return request.app.state.secret_box


def get_otp_broker(request: Request):
    return request.app.state.otp_broker
