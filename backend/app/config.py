"""აპლიკაციის კონფიგურაცია (გარემოს ცვლადებიდან).

საიდუმლოებები (DB პაროლი, KMS გასაღების ID) მოდის გარემოდან / secret manager-იდან,
არასდროს - კოდიდან ან git-იდან.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_", extra="ignore")

    # Literal: შეცდომით ჩაწერილი "production" არ უნდა "გაძვრეს" dev-ის შეღავათებით (მაგ. local KEK)
    env: Literal["dev", "staging", "prod"] = "dev"
    database_url: str = "postgresql://ai_accountant_app@localhost:5432/ai_accountant"
    redis_url: str = "redis://localhost:6379/0"

    # --- საიდუმლოებების დაშიფვრა (envelope encryption) ---
    # prod: "aws-kms" (ან GCP KMS / Vault Transit). dev: "local" - KEK გარემოს ცვლადიდან.
    key_provider: Literal["local", "aws-kms"] = "local"
    kms_key_id: str | None = None
    # base64-კოდირებული 32 ბაიტი; მხოლოდ dev/test. prod-ში ცარიელი უნდა იყოს.
    local_master_key: SecretStr | None = None

    # --- ავთენტიფიკაცია ---
    # dev: X-Tenant-Id / X-User-Id header-ები (მხოლოდ env=dev). oidc: JWT (Keycloak / Auth0 / Cognito).
    auth_mode: Literal["dev", "oidc"] = "dev"
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_algorithms: list[str] = ["RS256", "ES256"]
    oidc_leeway_seconds: int = Field(default=30, ge=0, le=300)
    # ეს როლები rs.ge-ზე დეკლარაციებს აგზავნის/ადასტურებს -> MFA სავალდებულოა (JWT `amr` claim, RFC 8176)
    oidc_mfa_roles: list[str] = ["owner", "accountant"]
    oidc_mfa_amr_values: list[str] = ["mfa"]

    # --- HTTP ---
    cors_origins: list[str] = []  # მაგ. ["https://app.example.ge"] - Next.js ფრონტენდი
    max_request_bytes: int = Field(default=26 * 1024 * 1024, ge=1024)  # ატვირთვის ლიმიტი + multipart overhead

    # --- LLM ნაგულისხმევი მოდელები (მომხმარებელს შეუძლია შეცვალოს UI-ში) ---
    # სია ყოველთვის მოწმდება პროვაიდერის /models endpoint-ით, როცა მომხმარებელი გასაღებს შეიყვანს.
    default_model_claude: str = "claude-opus-5"
    default_model_chatgpt: str = "gpt-6-sol"
    default_model_gemini: str = "gemini-3.8-flash"

    # --- rs.ge ---
    rsge_base_url: str = "https://eservice.rs.ge"
    rsge_headless: bool = True
    rsge_chromium_executable: str | None = None
    rsge_otp_timeout_seconds: int = 180
    evidence_dir: str = "./var/evidence"

    # ავტომატური გადაწყვეტილების ზღვარი: ამაზე დაბალი confidence -> ადამიანის განხილვა
    ai_auto_approve_confidence: float = Field(default=0.9, ge=0, le=1)

    @model_validator(mode="after")
    def _check_oidc(self) -> "Settings":
        if self.auth_mode == "oidc":
            missing = [n for n in ("oidc_issuer", "oidc_audience", "oidc_jwks_url") if not getattr(self, n)]
            if missing:
                raise ValueError(f"auth_mode=oidc requires {', '.join('APP_' + m.upper() for m in missing)}")
            from .security.oidc import check_algorithms

            check_algorithms(self.oidc_algorithms)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
