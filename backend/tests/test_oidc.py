"""OIDC JWT ვერიფიკაცია - ნამდვილი RS256 ხელმოწერებით, JWKS-ის ნაცვლად სტატიკური გასაღები (ქსელის გარეშე)."""

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.main import check_startup_settings
from app.security.oidc import AuthError, OidcVerifier, check_algorithms

ISSUER = "https://id.example.ge/realms/accountant"
AUDIENCE = "ai-accountant-api"


class StaticJwks:
    """PyJWKClient-ის ჩანაცვლება: ყოველთვის ერთი და იგივე საჯარო გასაღები."""

    def __init__(self, public_key):
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token):
        jwt.get_unverified_header(token)  # დაზიანებულ token-ზე DecodeError, როგორც ნამდვილში
        return SimpleNamespace(key=self.public_key)


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_token(key, **overrides) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "user-1", "iat": now, "exp": now + 300, "amr": ["pwd", "mfa"]}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def verifier(key) -> OidcVerifier:
    return OidcVerifier(issuer=ISSUER, audience=AUDIENCE, algorithms=["RS256"], leeway_seconds=0,
                        jwks_client=StaticJwks(key.public_key()))


async def test_valid_token_yields_subject_and_amr(rsa_key):
    claims = await verifier(rsa_key).verify(make_token(rsa_key))
    assert claims.subject == "user-1" and claims.amr == ("pwd", "mfa")


@pytest.mark.parametrize("overrides", [
    {"aud": "another-app"},
    {"iss": "https://evil.example"},
    {"exp": int(time.time()) - 10},
    {"sub": None},
])
async def test_invalid_claims_are_rejected(rsa_key, overrides):
    with pytest.raises(AuthError) as err:
        await verifier(rsa_key).verify(make_token(rsa_key, **overrides))
    assert err.value.status == 401


async def test_foreign_signature_is_rejected(rsa_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthError):
        await verifier(rsa_key).verify(make_token(other))


async def test_hmac_alg_confusion_with_public_key_is_rejected(rsa_key):
    """კლასიკური შეტევა: HS256 token, რომლის HMAC "საიდუმლოც" სერვერის (საჯარო) PEM გასაღებია."""
    pem = rsa_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    now = int(time.time())

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    payload = b64(json.dumps({"iss": ISSUER, "aud": AUDIENCE, "sub": "admin", "iat": now, "exp": now + 300,
                              "amr": ["mfa"]}).encode())
    signature = b64(hmac.new(pem, f"{header}.{payload}".encode(), hashlib.sha256).digest())
    with pytest.raises(AuthError):
        await verifier(rsa_key).verify(f"{header}.{payload}.{signature}")


async def test_garbage_token_is_rejected(rsa_key):
    with pytest.raises(AuthError):
        await verifier(rsa_key).verify("not-a-jwt")


def test_symmetric_algorithms_cannot_be_configured():
    with pytest.raises(ValueError):
        check_algorithms(["RS256", "HS256"])
    with pytest.raises(ValueError):
        check_algorithms([])


def test_non_dev_environment_refuses_to_start_with_dev_auth(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    with pytest.raises(RuntimeError, match="APP_AUTH_MODE=oidc"):
        check_startup_settings(Settings(_env_file=None))
