"""OIDC access token-ის (JWT) შემოწმება.

IdP (Keycloak / Auth0 / Cognito / Entra ID) გასცემს access token-ს; ჩვენ ვამოწმებთ:
* ხელმოწერას IdP-ის JWKS-ით (მხოლოდ ასიმეტრიული ალგორითმები - `none`/HS256 აკრძალულია,
  რათა "alg confusion" შეტევით საჯარო გასაღები საიდუმლოდ ვერ გამოიყენონ);
* `iss`, `aud`, `exp`, `nbf`, `iat` და `sub`-ის არსებობას;
* სურვილისამებრ - MFA-ს (`amr` შეიცავს "mfa"-ს, ან `acr` დაშვებულ სიაშია).

token ადასტურებს მხოლოდ "ვინ ხარ" (sub). tenant-ის წევრობა და როლი მოდის ჩვენი DB-დან
(tenant_members) - IdP-ის როლებს არ ვენდობით, რადგან ერთი ადამიანი სხვადასხვა კომპანიაში
სხვადასხვა როლით შეიძლება იყოს.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Protocol

import jwt

_ASYMMETRIC_ALGORITHMS = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}


class AuthenticationError(Exception):
    """token არასწორია/ვადაგასულია - 401. შეტყობინება მომხმარებელს დეტალების გარეშე მიეწოდება."""


class SigningKeyResolver(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...  # jwt.PyJWKClient-ის ინტერფეისი


@dataclass(frozen=True)
class VerifiedToken:
    subject: str
    claims: dict[str, Any]

    def has_mfa(self, accepted_acr: Collection[str] = ()) -> bool:
        amr = self.claims.get("amr") or []
        return "mfa" in amr or (self.claims.get("acr") in accepted_acr)


class OidcTokenVerifier:
    def __init__(self, *, issuer: str, audience: str, jwks_url: str | None = None,
                 algorithms: Collection[str] = ("RS256", "ES256"), leeway_seconds: int = 30,
                 key_resolver: SigningKeyResolver | None = None):
        bad = set(algorithms) - _ASYMMETRIC_ALGORITHMS
        if bad or not algorithms:
            raise ValueError(f"only asymmetric JWT algorithms are allowed, got {sorted(bad) or 'none'}")
        if key_resolver is None:
            if not jwks_url:
                raise ValueError("jwks_url is required")
            # გასაღებები ქეშდება; უცნობ `kid`-ზე (IdP-მა გასაღები შეცვალა) JWKS თავიდან იტვირთება
            key_resolver = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600, timeout=10)
        self._issuer = issuer
        self._audience = audience
        self._algorithms = list(algorithms)
        self._leeway = leeway_seconds
        self._keys = key_resolver

    def verify(self, token: str) -> VerifiedToken:
        """ბლოკირებადია (JWKS-ის ჩატვირთვა ქეშის გამოტოვებისას) - async კოდიდან asyncio.to_thread-ით."""
        try:
            key = self._keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=self._algorithms, audience=self._audience, issuer=self._issuer,
                leeway=self._leeway, options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except (jwt.PyJWTError, KeyError, ValueError) as exc:
            raise AuthenticationError(f"invalid access token ({type(exc).__name__})") from None
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise AuthenticationError("token has no subject")
        return VerifiedToken(subject=subject, claims=claims)


def build_token_verifier(settings) -> OidcTokenVerifier | None:
    if settings.auth_mode != "oidc":
        return None
    missing = [name for name in ("oidc_issuer", "oidc_audience", "oidc_jwks_url") if not getattr(settings, name)]
    if missing:
        raise RuntimeError(f"APP_AUTH_MODE=oidc requires {', '.join('APP_' + m.upper() for m in missing)}")
    return OidcTokenVerifier(issuer=settings.oidc_issuer, audience=settings.oidc_audience,
                             jwks_url=settings.oidc_jwks_url, algorithms=settings.oidc_algorithms)
