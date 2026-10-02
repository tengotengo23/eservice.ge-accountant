"""საიდუმლოებების (LLM API Keys, rs.ge პაროლები, სესიის cookie-ები) დაშიფვრა.

სქემა - Envelope Encryption:

    plaintext --AES-256-GCM(DEK, AAD)--> ciphertext          (ინახება DB-ში)
    DEK       --KMS/HSM KEK(AAD)-------> wrapped_dek         (ინახება DB-ში)
    KEK       არასდროს ტოვებს KMS-ს (AWS KMS / GCP KMS / HashiCorp Vault Transit)

* ყოველ ჩანაწერს აქვს საკუთარი, შემთხვევითი DEK - ერთი გასაღების გაჟონვა სხვებს არ ხსნის.
* AAD (Additional Authenticated Data) აკავშირებს შიფრტექსტს კონკრეტულ tenant-სა და ჩანაწერთან:
  თუ თავდამსხმელი DB-ში ერთი კლიენტის შიფრტექსტს მეორის სტრიქონში გადააკოპირებს,
  გაშიფვრა ჩავარდება (InvalidTag).
* გასაღების როტაცია: ახალი KEK -> ძველი wrapped_dek-ების re-wrap (plaintext-ს არ ვეხებით).
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12
_FORMAT_VERSION = 1


class DecryptionError(Exception):
    """შიფრტექსტი დაზიანებულია, ან AAD (tenant/ჩანაწერი) არ ემთხვევა."""


@dataclass(frozen=True)
class SecretContext:
    """ვის ეკუთვნის საიდუმლო - შედის AAD-ში."""

    tenant_id: str
    purpose: str  # "llm_api_key" | "rsge_password" | "rsge_session_state" ...
    record_id: str

    def aad(self) -> bytes:
        return f"v{_FORMAT_VERSION}|{self.tenant_id}|{self.purpose}|{self.record_id}".encode()


@dataclass(frozen=True)
class EncryptedSecret:
    ciphertext: bytes  # nonce || AES-GCM(ciphertext+tag)
    wrapped_dek: bytes
    kek_id: str
    version: int = _FORMAT_VERSION


class KeyProvider(Protocol):
    """KEK-ის მფლობელი. prod-ში - KMS; KEK აპლიკაციის მეხსიერებაში არასდროს ხვდება."""

    @property
    def kek_id(self) -> str: ...

    def wrap(self, dek: bytes, aad: bytes) -> bytes: ...

    def unwrap(self, wrapped: bytes, aad: bytes, kek_id: str) -> bytes: ...


class LocalKeyProvider:
    """მხოლოდ dev/test-ისთვის: KEK გარემოს ცვლადიდან. prod-ში გამოიყენეთ AwsKmsKeyProvider."""

    def __init__(self, master_keys: dict[str, bytes], active_kek_id: str):
        for kid, key in master_keys.items():
            if len(key) != 32:
                raise ValueError(f"KEK {kid!r} must be 32 bytes")
        if active_kek_id not in master_keys:
            raise ValueError("active KEK not in master_keys")
        self._keys = master_keys
        self._active = active_kek_id

    @classmethod
    def from_base64(cls, b64_key: str, kek_id: str = "local-1") -> LocalKeyProvider:
        return cls({kek_id: base64.b64decode(b64_key)}, kek_id)

    @property
    def kek_id(self) -> str:
        return self._active

    def wrap(self, dek: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(_NONCE_LEN)
        return nonce + AESGCM(self._keys[self._active]).encrypt(nonce, dek, aad)

    def unwrap(self, wrapped: bytes, aad: bytes, kek_id: str) -> bytes:
        key = self._keys.get(kek_id)
        if key is None:
            raise DecryptionError(f"unknown KEK {kek_id!r}")
        try:
            return AESGCM(key).decrypt(wrapped[:_NONCE_LEN], wrapped[_NONCE_LEN:], aad)
        except InvalidTag as exc:
            raise DecryptionError("DEK unwrap failed") from exc


class AwsKmsKeyProvider:
    """AWS KMS: KEK არასდროს ტოვებს HSM-ს. EncryptionContext = AAD (ჩანს CloudTrail-ში აუდიტისთვის).

    IAM პოლიტიკა: kms:Encrypt/kms:Decrypt მხოლოდ worker-ის როლს, EncryptionContext-ის პირობით.
    """

    def __init__(self, key_id: str, client=None):
        if client is None:
            import boto3  # optional dependency: pip install ai-accountant[aws]

            client = boto3.client("kms")
        self._client = client
        self._key_id = key_id

    @property
    def kek_id(self) -> str:
        return self._key_id

    def wrap(self, dek: bytes, aad: bytes) -> bytes:
        resp = self._client.encrypt(
            KeyId=self._key_id, Plaintext=dek, EncryptionContext={"aad": aad.decode()}
        )
        return resp["CiphertextBlob"]

    def unwrap(self, wrapped: bytes, aad: bytes, kek_id: str) -> bytes:
        try:
            resp = self._client.decrypt(
                KeyId=kek_id, CiphertextBlob=wrapped, EncryptionContext={"aad": aad.decode()}
            )
        except Exception as exc:  # botocore.exceptions.ClientError (InvalidCiphertextException, ...)
            raise DecryptionError("KMS decrypt failed") from exc
        return resp["Plaintext"]


class SecretBox:
    def __init__(self, key_provider: KeyProvider):
        self._kp = key_provider

    def encrypt(self, plaintext: str | bytes, context: SecretContext) -> EncryptedSecret:
        data = plaintext.encode() if isinstance(plaintext, str) else plaintext
        aad = context.aad()
        dek = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(_NONCE_LEN)
        ciphertext = nonce + AESGCM(dek).encrypt(nonce, data, aad)
        return EncryptedSecret(
            ciphertext=ciphertext, wrapped_dek=self._kp.wrap(dek, aad), kek_id=self._kp.kek_id
        )

    def decrypt_bytes(self, secret: EncryptedSecret, context: SecretContext) -> bytes:
        if secret.version != _FORMAT_VERSION:
            raise DecryptionError(f"unsupported secret format v{secret.version}")
        aad = context.aad()
        dek = self._kp.unwrap(secret.wrapped_dek, aad, secret.kek_id)
        ct = secret.ciphertext
        try:
            return AESGCM(dek).decrypt(ct[:_NONCE_LEN], ct[_NONCE_LEN:], aad)
        except InvalidTag as exc:
            raise DecryptionError("secret does not belong to this tenant/record") from exc

    def decrypt(self, secret: EncryptedSecret, context: SecretContext) -> str:
        return self.decrypt_bytes(secret, context).decode()

    def rewrap(self, secret: EncryptedSecret, context: SecretContext) -> EncryptedSecret:
        """KEK როტაცია: DEK-ს ვხსნით ძველი KEK-ით და ვფუთავთ აქტიურით. plaintext უცვლელია."""
        aad = context.aad()
        dek = self._kp.unwrap(secret.wrapped_dek, aad, secret.kek_id)
        return EncryptedSecret(
            ciphertext=secret.ciphertext, wrapped_dek=self._kp.wrap(dek, aad), kek_id=self._kp.kek_id
        )


def key_hint(secret: str, visible: int = 4) -> str:
    """UI-სთვის: 'sk-ant-api03-...' -> '…Xy9Z'. სრული მნიშვნელობა API-დან არასდროს ბრუნდება."""
    return "…" + secret[-visible:] if len(secret) > visible * 2 else "…"


def build_key_provider(settings) -> KeyProvider:
    if settings.key_provider == "aws-kms":
        if not settings.kms_key_id:
            raise RuntimeError("APP_KMS_KEY_ID is required for aws-kms")
        return AwsKmsKeyProvider(settings.kms_key_id)
    if settings.key_provider == "local":
        if settings.env == "prod":
            raise RuntimeError("local key provider is not allowed in prod")
        if settings.local_master_key is None:
            raise RuntimeError("APP_LOCAL_MASTER_KEY is required for the local key provider")
        return LocalKeyProvider.from_base64(settings.local_master_key.get_secret_value())
    raise RuntimeError(f"unknown key provider {settings.key_provider!r}")
