"""გადასახადის გადამხდელები (კომპანიები/მეწარმეები) და მათი rs.ge წვდომის მონაცემები.

rs.ge პაროლი და Playwright-ის სესია (cookies) ინახება მხოლოდ დაშიფრულად (envelope encryption, AAD =
tenant | purpose | ჩანაწერის id). პაროლი API-დან არასდროს ბრუნდება - მხოლოდ `username_hint`.
სესიის შენახვა ამცირებს SMS-ის საჭიროებას: ხელახალი ავტორიზაცია მხოლოდ სესიის ვადის გასვლისას.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

import psycopg

from ..db.database import Database, audit
from ..rsge.browser import RsGeCredentials
from ..security.crypto import EncryptedSecret, SecretBox, SecretContext

PASSWORD_PURPOSE = "rsge_password"  # noqa: S105 - AAD-ის purpose ჭდე, არა პაროლი
SESSION_PURPOSE = "rsge_session_state"


class LegalForm(str, Enum):
    LLC = "llc"
    JSC = "jsc"
    INDIVIDUAL_ENTREPRENEUR = "individual_entrepreneur"
    INDIVIDUAL = "individual"
    NGO = "ngo"
    BRANCH = "branch"
    OTHER = "other"


class CredentialKind(str, Enum):
    PORTAL_USER = "portal_user"  # eservice.rs.ge (ბრაუზერი, SMS)
    SERVICE_USER = "service_user"  # SOAP სერვისების მომხმარებელი


class TaxpayerNotFoundError(LookupError):
    pass


class TaxpayerExistsError(ValueError):
    pass


class RsgeCredentialUnavailableError(LookupError):
    def __init__(self, status: str | None):
        super().__init__(f"no usable rs.ge credential (status: {status or 'missing'})")
        self.status = status


@dataclass(frozen=True)
class TaxpayerView:
    id: UUID
    tin: str
    legal_name: str
    legal_form: str
    regimes: list[str]
    vat_registered_on: date | None
    has_employees: bool
    created_at: datetime


@dataclass(frozen=True)
class RsgeCredentialView:
    id: UUID
    taxpayer_id: UUID
    kind: str
    username_hint: str
    status: str
    has_session: bool
    last_login_at: datetime | None
    last_error_code: str | None
    created_at: datetime


@dataclass(frozen=True)
class LoadedRsgeCredential:
    """worker-ისთვის: გაშიფრული მონაცემები მხოლოდ მეხსიერებაში, დავალების ხანგრძლივობით."""

    id: UUID
    credentials: RsGeCredentials
    storage_state: dict[str, Any] | None


_TAXPAYER_COLUMNS = ("id, tin, legal_name, legal_form::text AS legal_form, regimes::text[] AS regimes,"
                     " vat_registered_on, has_employees, created_at")
_CRED_COLUMNS = ("id, taxpayer_id, kind::text AS kind, username_hint, status::text AS status,"
                 " session_ciphertext IS NOT NULL AS has_session, last_login_at, last_error_code, created_at")


def username_hint(username: str) -> str:
    """'405123456' -> '405***56'. მომხმარებლის სახელიც ნაწილობრივ იფარება (ხშირად ეს პირადი ნომერია)."""
    username = username.strip()
    return username[:3] + "***" + username[-2:] if len(username) > 6 else "***"


# ------------------------------------------------------------------ taxpayers


async def create_taxpayer(db: Database, *, tenant_id: UUID, user_id: UUID, tin: str, legal_name: str,
                          legal_form: LegalForm, regimes: list[str], has_employees: bool = False,
                          vat_registered_on: date | None = None, ip: str | None = None) -> TaxpayerView:
    try:
        async with db.tenant_tx(tenant_id, actor_id=user_id) as conn:
            row = await (await conn.execute(
                f"INSERT INTO taxpayers (tenant_id, tin, legal_name, legal_form, regimes, has_employees,"
                f" vat_registered_on) VALUES (%s, %s, %s, %s, %s::tax_regime[], %s, %s) RETURNING {_TAXPAYER_COLUMNS}",
                (tenant_id, tin, legal_name, legal_form.value, sorted(set(regimes)), has_employees,
                 vat_registered_on))).fetchone()
            await audit(conn, tenant_id=tenant_id, actor_type="user", actor_id=user_id, action="taxpayer.created",
                        entity_type="taxpayer", entity_id=str(row["id"]), ip=ip,
                        details={"tin": tin, "regimes": row["regimes"]})
    except psycopg.errors.UniqueViolation:
        raise TaxpayerExistsError(tin) from None
    return TaxpayerView(**row)


async def list_taxpayers(db: Database, tenant_id: UUID) -> list[TaxpayerView]:
    async with db.tenant_tx(tenant_id) as conn:
        rows = await (await conn.execute(f"SELECT {_TAXPAYER_COLUMNS} FROM taxpayers ORDER BY legal_name")).fetchall()
    return [TaxpayerView(**r) for r in rows]


# ------------------------------------------------------------------ rs.ge credentials


async def set_rsge_credential(db: Database, box: SecretBox, *, tenant_id: UUID, user_id: UUID, taxpayer_id: UUID,
                              kind: CredentialKind, username: str, password: str,
                              ip: str | None = None) -> RsgeCredentialView:
    """შექმნა ან ჩანაცვლება. ახალი პაროლი ძველ სესიას აუქმებს (სხვა ანგარიშის cookies არ უნდა დარჩეს)."""
    username = username.strip()
    async with db.tenant_tx(tenant_id, actor_id=user_id) as conn:
        if await (await conn.execute("SELECT 1 FROM taxpayers WHERE id = %s", (taxpayer_id,))).fetchone() is None:
            raise TaxpayerNotFoundError(str(taxpayer_id))
        existing = await (await conn.execute(
            "SELECT id FROM rsge_credentials WHERE taxpayer_id = %s AND kind = %s FOR UPDATE",
            (taxpayer_id, kind.value))).fetchone()
        cred_id = existing["id"] if existing else uuid4()
        payload = json.dumps({"username": username, "password": password})
        secret = await asyncio.to_thread(box.encrypt, payload, SecretContext(str(tenant_id), PASSWORD_PURPOSE,
                                                                             str(cred_id)))
        values = (secret.ciphertext, secret.wrapped_dek, secret.kek_id, username_hint(username))
        if existing:
            await conn.execute(
                "UPDATE rsge_credentials SET secret_ciphertext = %s, secret_wrapped_dek = %s, secret_kek_id = %s,"
                " username_hint = %s, status = 'active', revoked_at = NULL, last_error_code = NULL,"
                " session_ciphertext = NULL, session_wrapped_dek = NULL, session_kek_id = NULL,"
                " session_expires_at = NULL WHERE id = %s", (*values, cred_id))
        else:
            await conn.execute(
                "INSERT INTO rsge_credentials (id, tenant_id, taxpayer_id, kind, secret_ciphertext, secret_wrapped_dek,"
                " secret_kek_id, username_hint, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (cred_id, tenant_id, taxpayer_id, kind.value, *values, user_id))
        row = await (await conn.execute(f"SELECT {_CRED_COLUMNS} FROM rsge_credentials WHERE id = %s",
                                        (cred_id,))).fetchone()
        await audit(conn, tenant_id=tenant_id, actor_type="user", actor_id=user_id, action="rsge_credential.set",
                    entity_type="rsge_credential", entity_id=str(cred_id), ip=ip,
                    details={"taxpayer_id": str(taxpayer_id), "kind": kind.value, "username_hint": values[3],
                             "replaced": existing is not None})
    return RsgeCredentialView(**row)


async def list_rsge_credentials(db: Database, tenant_id: UUID, taxpayer_id: UUID) -> list[RsgeCredentialView]:
    async with db.tenant_tx(tenant_id) as conn:
        rows = await (await conn.execute(
            f"SELECT {_CRED_COLUMNS} FROM rsge_credentials WHERE taxpayer_id = %s AND status <> 'revoked'"
            " ORDER BY kind", (taxpayer_id,))).fetchall()
    return [RsgeCredentialView(**r) for r in rows]


async def revoke_rsge_credential(db: Database, *, tenant_id: UUID, user_id: UUID, taxpayer_id: UUID,
                                 kind: CredentialKind, ip: str | None = None) -> bool:
    async with db.tenant_tx(tenant_id, actor_id=user_id) as conn:
        row = await (await conn.execute(
            "UPDATE rsge_credentials SET status = 'revoked', revoked_at = now(), secret_ciphertext = ''::bytea,"
            " secret_wrapped_dek = ''::bytea, session_ciphertext = NULL, session_wrapped_dek = NULL,"
            " session_kek_id = NULL, session_expires_at = NULL"
            " WHERE taxpayer_id = %s AND kind = %s AND status <> 'revoked' RETURNING id",
            (taxpayer_id, kind.value))).fetchone()
        if row:
            await audit(conn, tenant_id=tenant_id, actor_type="user", actor_id=user_id,
                        action="rsge_credential.revoked", entity_type="rsge_credential", entity_id=str(row["id"]),
                        ip=ip, details={"taxpayer_id": str(taxpayer_id), "kind": kind.value})
    return row is not None


async def load_portal_credential(db: Database, box: SecretBox, tenant_id: UUID,
                                 taxpayer_id: UUID) -> LoadedRsgeCredential:
    async with db.tenant_tx(tenant_id, actor_type="worker") as conn:
        row = await (await conn.execute(
            "SELECT id, status::text AS status, secret_ciphertext, secret_wrapped_dek, secret_kek_id,"
            " session_ciphertext, session_wrapped_dek, session_kek_id FROM rsge_credentials"
            " WHERE taxpayer_id = %s AND kind = 'portal_user'", (taxpayer_id,))).fetchone()
    if row is None or row["status"] != "active":
        raise RsgeCredentialUnavailableError(row["status"] if row else None)
    cred_id = str(row["id"])
    secret = EncryptedSecret(bytes(row["secret_ciphertext"]), bytes(row["secret_wrapped_dek"]), row["secret_kek_id"])
    data = json.loads(await asyncio.to_thread(box.decrypt, secret,
                                              SecretContext(str(tenant_id), PASSWORD_PURPOSE, cred_id)))
    storage_state = None
    if row["session_ciphertext"] is not None:
        session = EncryptedSecret(bytes(row["session_ciphertext"]), bytes(row["session_wrapped_dek"]),
                                  row["session_kek_id"])
        storage_state = json.loads(await asyncio.to_thread(box.decrypt, session,
                                                           SecretContext(str(tenant_id), SESSION_PURPOSE, cred_id)))
    return LoadedRsgeCredential(
        id=row["id"], storage_state=storage_state,
        credentials=RsGeCredentials(tenant_id=str(tenant_id), credential_id=cred_id, username=data["username"],
                                    password=data["password"]))


async def save_session_state(db: Database, box: SecretBox, *, tenant_id: UUID, credential_id: UUID,
                             storage_state: dict[str, Any]) -> None:
    secret = await asyncio.to_thread(box.encrypt, json.dumps(storage_state),
                                     SecretContext(str(tenant_id), SESSION_PURPOSE, str(credential_id)))
    async with db.tenant_tx(tenant_id, actor_type="worker") as conn:
        await conn.execute(
            "UPDATE rsge_credentials SET session_ciphertext = %s, session_wrapped_dek = %s, session_kek_id = %s,"
            " last_login_at = now(), last_error_code = NULL WHERE id = %s AND status = 'active'",
            (secret.ciphertext, secret.wrapped_dek, secret.kek_id, credential_id))


async def mark_credential_problem(db: Database, *, tenant_id: UUID, credential_id: UUID, error_code: str,
                                  status: str | None = None) -> None:
    """არასწორი პაროლი -> 'invalid' (ხელახლა აღარ ვცდით, ანგარიშის დაბლოკვის რისკი). სესია იშლება."""
    async with db.tenant_tx(tenant_id, actor_type="worker") as conn:
        await conn.execute(
            "UPDATE rsge_credentials SET last_error_code = %s, status = coalesce(%s::credential_status, status),"
            " session_ciphertext = CASE WHEN %s::text IS NULL THEN session_ciphertext END,"
            " session_wrapped_dek = CASE WHEN %s::text IS NULL THEN session_wrapped_dek END,"
            " session_kek_id = CASE WHEN %s::text IS NULL THEN session_kek_id END"
            " WHERE id = %s AND status <> 'revoked'",
            (error_code, status, status, status, status, credential_id))
