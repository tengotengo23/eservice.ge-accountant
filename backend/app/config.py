"""აპლიკაციის კონფიგურაცია (გარემოს ცვლადებიდან).

საიდუმლოებები (DB პაროლი, KMS გასაღების ID) მოდის გარემოდან / secret manager-იდან,
არასდროს - კოდიდან ან git-იდან.
"""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_", extra="ignore")

    env: str = "dev"  # dev | staging | prod
    database_url: str = "postgresql://ai_accountant_app@localhost:5432/ai_accountant"
    redis_url: str = "redis://localhost:6379/0"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
