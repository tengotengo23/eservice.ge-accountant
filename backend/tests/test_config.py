import pytest
from pydantic import ValidationError

from app.config import Settings
from app.security.crypto import build_key_provider


def test_misspelled_env_is_rejected_instead_of_silently_allowing_dev_shortcuts(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_local_key_provider_is_forbidden_in_prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("APP_LOCAL_MASTER_KEY", "A" * 43 + "=")
    with pytest.raises(RuntimeError, match="not allowed in prod"):
        build_key_provider(Settings(_env_file=None))


def test_oidc_mode_requires_issuer_audience_and_jwks(monkeypatch):
    monkeypatch.setenv("APP_AUTH_MODE", "oidc")
    monkeypatch.setenv("APP_OIDC_ISSUER", "https://id.example.ge/realms/app")
    with pytest.raises(ValidationError, match="APP_OIDC_AUDIENCE"):
        Settings(_env_file=None)
