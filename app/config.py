from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "Conserve Mass Ops"
APP_VERSION = "0.4.3"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    config_dir: Path = Path("/config")
    base_url: str = "http://localhost:8000"

    esi_client_id: str = ""
    esi_client_secret: str = ""
    esi_contact: str = ""

    admin_character_ids: str = ""
    corporation_id: int | None = None

    session_secret: str = ""
    token_encryption_key: str = ""

    discord_bot_token: str = ""
    discord_owner_user_id: int | None = None

    # How often to *check*; ESI is only called once its cache (Expires header) lapses.
    contract_poll_seconds: int = 60
    expiry_warning_hours: int = 24
    backup_keep_days: int = 14
    log_level: str = "INFO"

    # GitHub repo checked for new versions (owner/name).
    update_repo: str = "cmasiah/conserve-mass"

    # --- abuse / spam protection ---
    # Auto-mute an issuer who posts more than this many new contracts within an hour.
    spam_contracts_per_hour: int = 10
    # More than this many pending alerts of one kind are merged into a single digest DM.
    digest_threshold: int = 3
    # Hard cap on Discord messages per hour; anything beyond waits and gets digested.
    discord_max_messages_per_hour: int = 30
    # New location lookups per sync (each unknown structure is one ESI call).
    max_location_lookups_per_sync: int = 25

    @field_validator("corporation_id", "discord_owner_user_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        if v is None or (isinstance(v, str) and not v.strip().strip('"\'')):
            return None
        return v.strip().strip('"\'') if isinstance(v, str) else v

    @field_validator("*", mode="before")
    @classmethod
    def _strip_whitespace(cls, v):
        # .env files edited on Windows carry \r; quotes sometimes get pasted too.
        if isinstance(v, str):
            return v.strip().strip('"').strip("'").strip()
        return v

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.admin_character_ids.replace(" ", "").split(",") if x}

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.config_dir / 'conserve-mass.db'}"

    @property
    def backup_dir(self) -> Path:
        return self.config_dir / "backups"

    @property
    def sso_callback_url(self) -> str:
        return f"{self.base_url}/auth/callback"

    @property
    def user_agent(self) -> str:
        # CCP asks for app/version plus a contact; email strongly preferred.
        parts = [p for p in (self.esi_contact, f"+https://github.com/{self.update_repo}") if p]
        return f"ConserveMassOps/{APP_VERSION} ({'; '.join(parts)})"

    def missing_required(self) -> list[str]:
        required = {
            "ESI_CLIENT_ID": self.esi_client_id,
            "ESI_CLIENT_SECRET": self.esi_client_secret,
            "SESSION_SECRET": self.session_secret,
            "TOKEN_ENCRYPTION_KEY": self.token_encryption_key,
        }
        return [k for k, v in required.items() if not v]


@lru_cache
def get_settings() -> Settings:
    return Settings()
