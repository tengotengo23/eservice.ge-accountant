"""აპლიკაციის კონფიგურაცია (გარემოს ცვლადებიდან).

საიდუმლოებები (DB პაროლი, KMS გასაღების ID) მოდის გარემოდან / secret manager-იდან,
არასდროს - კოდიდან ან git-იდან.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_", extra="ignore")

    # მკაცრი სია: "production"-ის მსგავსი შეცდომით დაწერილი მნიშვნელობა prod-ის შემოწმებებს ჩუმად არ გვერდს აუვლის
    env: Literal["dev", "test", "staging", "prod"] = "dev"
    database_url: str = "postgresql://ai_accountant_app@localhost:5432/ai_accountant"
    redis_url: str = "redis://localhost:6379/0"

    # --- ავთენტიფიკაცია ---
    # dev_headers: X-Tenant-Id + X-User-Id (მხოლოდ APP_ENV=dev). oidc: Authorization: Bearer <JWT> + X-Tenant-Id.
    # ორივე რეჟიმში როლი მოდის tenant_members-იდან (DB), და არა კლიენტისგან.
    auth_mode: Literal["dev_headers", "oidc"] = "dev_headers"
    oidc_issuer: str | None = None  # მაგ. https://auth.example.ge/realms/ai-accountant
    oidc_audience: str | None = None  # API-ის client id / audience
    oidc_jwks_url: str | None = None  # Keycloak: {issuer}/protocol/openid-connect/certs
    oidc_algorithms: list[str] = ["RS256", "ES256"]
    # owner/accountant აგზავნიან დეკლარაციებს rs.ge-ზე - მათთვის MFA სავალდებულო უნდა იყოს (IdP-ის მხარეს).
    # ჩართვისას token-ში უნდა იყოს amr=["mfa",...] ან acr ქვემოთ მოცემული სიიდან.
    oidc_require_mfa_for_staff: bool = False
    oidc_mfa_acr_values: list[str] = []

    # --- საიდუმლოებების დაშიფვრა (envelope encryption) ---
    # prod: "aws-kms" (ან GCP KMS / Vault Transit). dev: "local" - KEK გარემოს ცვლადიდან.
    key_provider: str = "local"
    kms_key_id: str | None = None
    # base64-კოდირებული 32 ბაიტი; მხოლოდ dev/test. prod-ში ცარიელი უნდა იყოს.
    local_master_key: SecretStr | None = None

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

    @field_validator("local_master_key", "kms_key_id", "oidc_issuer", "oidc_audience", "oidc_jwks_url",
                     mode="before")
    @classmethod
    def _blank_is_unset(cls, value):
        # `.env.example`-დან დაკოპირებული `APP_LOCAL_MASTER_KEY=` ცარიელი სტრიქონია და არა "არ არის მითითებული"
        return None if isinstance(value, str) and not value.strip() else value


@lru_cache
def get_settings() -> Settings:
    return Settings()


def check_startup_settings(settings: Settings) -> None:
    """დაუშვებელი კომბინაციები აპლიკაციის გაშვებისთანავე ჩერდება (და არა პირველ მოთხოვნაზე)."""
    if settings.auth_mode == "dev_headers" and settings.env != "dev":
        raise RuntimeError("APP_AUTH_MODE=dev_headers is allowed only with APP_ENV=dev; configure OIDC")
