import logging
import os

import pytest

from app.security.crypto import (
    DecryptionError,
    LocalKeyProvider,
    SecretBox,
    SecretContext,
    build_key_provider,
    key_hint,
)
from app.security.redaction import RedactingFilter, redact


@pytest.fixture
def box():
    return SecretBox(LocalKeyProvider({"k1": os.urandom(32)}, "k1"))


CTX = SecretContext(tenant_id="t-1", purpose="llm_api_key", record_id="cred-1")


def test_roundtrip_and_fresh_dek_per_secret(box):
    a = box.encrypt("sk-ant-api03-SECRET", CTX)
    b = box.encrypt("sk-ant-api03-SECRET", CTX)
    assert box.decrypt(a, CTX) == "sk-ant-api03-SECRET"
    assert a.ciphertext != b.ciphertext and a.wrapped_dek != b.wrapped_dek
    assert b"SECRET" not in a.ciphertext


def test_ciphertext_cannot_be_moved_to_another_tenant_or_record(box):
    secret = box.encrypt("password123", CTX)
    for other in (
        SecretContext("t-2", "llm_api_key", "cred-1"),
        SecretContext("t-1", "rsge_password", "cred-1"),
        SecretContext("t-1", "llm_api_key", "cred-2"),
    ):
        with pytest.raises(DecryptionError):
            box.decrypt(secret, other)


def test_kek_rotation_rewraps_without_touching_ciphertext():
    old_key, new_key = os.urandom(32), os.urandom(32)
    old_box = SecretBox(LocalKeyProvider({"k1": old_key}, "k1"))
    secret = old_box.encrypt("rs.ge-pass", CTX)

    rotated_box = SecretBox(LocalKeyProvider({"k1": old_key, "k2": new_key}, "k2"))
    rotated = rotated_box.rewrap(secret, CTX)
    assert rotated.kek_id == "k2" and rotated.ciphertext == secret.ciphertext

    new_only = SecretBox(LocalKeyProvider({"k2": new_key}, "k2"))
    assert new_only.decrypt(rotated, CTX) == "rs.ge-pass"
    with pytest.raises(DecryptionError):
        new_only.decrypt(secret, CTX)  # ძველი KEK-ით შეფუთული - ძველი KEK-ის გარეშე ვერ იხსნება


def test_key_hint_never_reveals_secret():
    assert key_hint("sk-ant-api03-abcdefghijkl") == "…ijkl"
    assert key_hint("short") == "…"


def test_redaction_masks_keys_and_passwords(caplog):
    text = "calling with sk-ant-api03-AAAAAAAAAAAA and password=hunter2 and AIzaSyA1234567890123456789012345678901"
    out = redact(text)
    assert "AAAAAAAAAAAA" not in out and "hunter2" not in out and "AIzaSy" not in out

    logger = logging.getLogger("test.redaction")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="test.redaction"):
        logger.info("user key %s", "sk-proj-ZZZZZZZZZZZZZZZZZZZZ")
    assert "ZZZZ" not in caplog.text


def test_redaction_covers_tracebacks_and_auth_headers(caplog):
    logger = logging.getLogger("test.redaction.exc")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.ERROR, logger="test.redaction.exc"):
        try:
            raise RuntimeError("provider said: invalid key sk-ant-api03-LEAKEDLEAKED")
        except RuntimeError:
            logger.exception("call failed")
    assert "LEAKEDLEAKED" not in caplog.text and "RuntimeError" in caplog.text

    out = redact("Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig x-api-key: sk-ant-api03-ZZZZZZZZ")
    assert "eyJhbGci" not in out and "ZZZZZZZZ" not in out
    assert redact("token: Bearer abcdefgh12345678") == "token: Bearer ***"


def test_key_provider_config_errors_are_explicit(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("APP_LOCAL_MASTER_KEY", "")  # .env.example-დან დაკოპირებული ცარიელი მნიშვნელობა
    with pytest.raises(RuntimeError, match="APP_LOCAL_MASTER_KEY is required"):
        build_key_provider(Settings(_env_file=None))
    monkeypatch.setenv("APP_LOCAL_MASTER_KEY", "bm90LTMyLWJ5dGVz")  # "not-32-bytes"
    with pytest.raises(ValueError, match="32 bytes"):
        build_key_provider(Settings(_env_file=None))
    monkeypatch.setenv("APP_ENV", "production")  # არასწორი სახელი არ უნდა "გახდეს" dev
    with pytest.raises(ValueError):
        Settings(_env_file=None)
