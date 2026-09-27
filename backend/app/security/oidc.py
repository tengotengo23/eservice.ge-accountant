"""OIDC ავთენტიფიკაცია (Keycloak / Auth0 / Cognito): JWT-ის ვერიფიკაცია JWKS-ით.

მოწმდება: ხელმოწერა (მხოლოდ ასიმეტრიული ალგორითმები - "alg confusion"-ის წინააღმდეგ), iss, aud,
exp / iat / nbf (მცირე leeway-თ). როლი JWT-დან კი არ მოდის, არამედ DB-დან (tenant_members) - ასე
IdP-ში როლის შეცვლა/წართმევა მყისიერად მოქმედებს და ერთი ადამიანი რამდენიმე კომპანიის წევრი შეიძლება იყოს.

MFA: JWT-ის `amr` claim (RFC 8176) - owner/accountant როლებს ის სავალდებულოდ სჭირდებათ (იხ. config).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

import jwt

_ASYMMETRIC_PREFIXES = ("RS", "PS", "ES", "EdDSA")


class AuthError(Exception):
    def __init__(self, message: str, *, status: int = 401):
        super().__init__(message)
        self.status = status


class SigningKeySource(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    amr: tuple[str, ...]
    raw: dict[str, Any] = field(repr=False)


def check_algorithms(algorithms: list[str]) -> list[str]:
    bad = [a for a in algorithms if not a.startswith(_ASYMMETRIC_PREFIXES)]
    if not algorithms or bad:
        raise ValueError(f"only asymmetric JWT algorithms are allowed (got {bad or 'none'})")
    return algorithms


class OidcVerifier:
    def __init__(self, *, issuer: str, audience: str, algorithms: list[str], jwks_url: str | None = None,
                 leeway_seconds: int = 30, jwks_client: SigningKeySource | None = None):
        if jwks_client is None:
            if not jwks_url:
                raise ValueError("jwks_url is required")
            # გასაღებები ქეშდება; უცნობ `kid`-ზე (IdP-მ როტაცია გააკეთა) JWKS ხელახლა ჩამოიტვირთება
            jwks_client = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600, timeout=10)
        self._jwks = jwks_client
        self._issuer = issuer
        self._audience = audience
        self._algorithms = check_algorithms(list(algorithms))
        self._leeway = leeway_seconds

    async def verify(self, token: str) -> TokenClaims:
        try:
            # PyJWKClient სინქრონულად (urllib) იღებს JWKS-ს - event loop-ს არ ვბლოკავთ
            signing_key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token, signing_key.key, algorithms=self._algorithms, audience=self._audience, issuer=self._issuer,
                leeway=self._leeway, options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWKClientConnectionError:
            raise AuthError("identity provider is unreachable", status=503) from None
        except jwt.PyJWKClientError:
            raise AuthError("token signing key is unknown") from None
        except jwt.InvalidTokenError as exc:
            raise AuthError(f"invalid token ({type(exc).__name__})") from None
        amr = claims.get("amr") or []
        if isinstance(amr, str):
            amr = [amr]
        return TokenClaims(subject=str(claims["sub"]), amr=tuple(str(a) for a in amr), raw=claims)
